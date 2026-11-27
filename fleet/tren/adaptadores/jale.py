"""Recibos jale-recibo@1; sin juicio contra base ni normalización de firmas."""
import base64
import binascii
import fcntl
import hashlib
from itertools import chain, combinations, islice
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

from adaptadores.ejecucion_jale import (ACUSE_CANCELACION, CANCELAR, clave_iniciada,
                                       clave_registro, guardar)
from adaptadores.hasito import ResultadoInvalido, exigir, leer_json
from atribucion import id_reproduccion, identidad
from dominio import (Completo, Diagnosticando, Fallo, Incompleto, Intento, Repeticion,
                     UnidadRepetida, RechazoLanzador, clave)
from git_ops import Publicado, sha
from ledger import directorio_estado
from operador import lanzamiento_activo


MODOS = ('contract', 'e2e')
HOSTS = ('auto', 'mini', 'desktop', 'boat')
ESPERA_BOAT_S = 180
PLAZO_PRELANZAMIENTO_S = 5
PLAZO_CANCELACION_S = 30
SIN_IMPEDIMENTO = object()
EN_CURSO = object()


def especulativa(registro):
    return ((registro / 'prelanzada.json').exists() and
            not (registro / 'confirmada.json').exists())


def con_candado(registro):
    try:
        with (registro / 'candado').open('rb') as candado:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True
    return False


def corrida_en_curso(registro):
    return (lanzamiento_activo(registro / 'candado') and
            not (registro / CANCELAR).exists() and
            not (registro / 'codigo.json').exists())


def aviso_prelanzado(registro, tren_id, modo):
    try:
        iniciado = leer_json(registro / 'iniciado.json')
    except ResultadoInvalido:
        iniciado = None
    return {'tren': tren_id, 'modo': modo, 'clave': clave_iniciada(iniciado),
            'terminada': (registro / 'codigo.json').exists()}


def leer_pendiente_boat(log):
    sidecar = log.with_name(log.name + '.boat-pendiente')
    if not sidecar.exists():
        return None
    pendiente = leer_json(sidecar)
    exigir(isinstance(pendiente, dict) and pendiente.get('status') == 'pending' and
           isinstance(pendiente.get('cause'), str), 'pendiente-boat-invalido')
    return pendiente


def identidad_boat_pendiente(registro):
    codigo = registro / 'codigo.json'
    if not codigo.exists() or leer_json(codigo) != 75:
        return None
    archivo = registro / 'entorno.json'
    if not archivo.exists():
        return None
    entorno = leer_json(archivo)
    identidad = entorno.get('SEN_RUN_ID') if isinstance(entorno, dict) else None
    if identidad is None:
        return None
    exigir(isinstance(identidad, str) and re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', identidad), 'pendiente-boat-invalido')
    log = registro / 'corrida.log'
    pendiente = leer_pendiente_boat(log)
    if pendiente is not None:
        # Boat conserva indefinidamente estas salidas sin un resultado recuperable.
        if re.search(r'\b(?:interrupted|interrumpid[ao]s?|resultado desconocido|sin VM)\b|\bcancelaci',
                     pendiente['cause'], re.IGNORECASE):
            return None
        return identidad
    # Un 75 del desk en el fallback no deja sidecar ni RECIBO ni RESULT.
    if log.exists():
        lineas = log.read_text(encoding='utf-8', errors='replace').splitlines()
        if (any(linea.startswith('HOST=desktop fallback=') for linea in lineas) and
                not any(linea.startswith(('RECIBO ', 'RESULT ')) for linea in lineas)):
            return identidad
    return None


def objeto(pares):
    resultado = {}
    for k, v in pares:
        if k in resultado:
            raise ValueError('campo duplicado')
        resultado[k] = v
    return resultado


def leer_recibo(log, codigo, commit, modo, filtros, *, host):
    exigir(type(codigo) is int, 'salida-no-utilizable')
    if codigo == 64:
        return RechazoLanzador(str(Path(log).with_name('coordinador.log')))
    try:
        # Boat y un 75 del desk pueden traer salida de pruebas con bytes no UTF-8.
        errores = 'replace' if host == 'boat' or codigo == 75 else 'strict'
        lineas = Path(log).read_text(encoding='utf-8', errors=errores).splitlines()
    except OSError:
        lineas = []
    except UnicodeError:
        raise ResultadoInvalido('recibo-malformado') from None
    if any(re.fullmatch(r'PARTIAL: boat omitted=0*[1-9][0-9]*', linea) for linea in lineas):
        return RechazoLanzador(str(log), 'cobertura-parcial-boat')
    recibos = [linea for linea in lineas if linea.startswith('RECIBO')]
    if not recibos:
        if codigo == 75:
            return None
        coordinador = str(Path(log).with_name('coordinador.log'))
        if codigo == 125 and 'PUEUE_INTERRUPTED=1' in lineas:
            return RechazoLanzador(coordinador, 'corrida-interrumpida')
        return RechazoLanzador(coordinador, 'salida-sin-recibo')
    exigir(len(recibos) == 1, 'salida-sin-recibo')
    exigir(0 <= codigo <= 255, 'salida-no-utilizable')
    match = re.fullmatch(r'RECIBO jale-recibo@1 ([A-Za-z0-9+/]+={0,2})', recibos[0])
    exigir(match is not None, 'recibo-malformado')
    try:
        r = json.loads(base64.b64decode(match[1], validate=True).decode('utf-8'),
                       object_pairs_hook=objeto)
    except (ValueError, UnicodeError, binascii.Error):
        raise ResultadoInvalido('recibo-malformado') from None
    exigir(isinstance(r, dict) and r.get('esquema') == 'jale-recibo@1', 'esquema-desconocido')
    exigir(sha(commit) and r.get('sha') == commit, 'sha-no-corresponde')
    exigir(r.get('modo') == modo, 'modo-no-corresponde')
    exigir(r.get('alcance') == ('parcial' if filtros else 'completo') and
           r.get('filtros') == list(filtros), 'seleccion-no-corresponde')
    exigir(type(r.get('codigo')) is int and r['codigo'] == codigo, 'salida-no-corresponde')
    exigir(type(r.get('completo')) is bool and isinstance(r.get('unidades'), list) and bool(r['unidades']))
    huella = r.get('huella_entorno')
    exigir(isinstance(huella, dict) and {'bun', 'node', 'playwright'}.issubset(huella) and all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in huella.items()),
        'entorno-invalido')
    rutas = []
    for u in r['unidades']:
        exigir(isinstance(u, dict))
        ruta = u.get('unidad')
        exigir(isinstance(ruta, str) and ruta and not PurePosixPath(ruta).is_absolute() and
               str(PurePosixPath(ruta)) == ruta and '..' not in PurePosixPath(ruta).parts and
               '\\' not in ruta, 'unidad-invalida')
        exigir((modo == 'contract' and ruta.endswith('.contract.test.ts')) or
               (modo == 'e2e' and ruta.startswith('apps/web/e2e/')), 'unidad-invalida')
        rutas.append(ruta)
        exigir(u.get('resultado') in ('pasa', 'falla', 'no-corrida'))
        falla = u['resultado'] == 'falla'
        duracion = u.get('duracion_s')
        exigir(type(u.get('fallidas')) is int and u['fallidas'] >= 0 and
               (u['fallidas'] > 0) == falla and type(duracion) in (int, float) and
               0 <= duracion <= sys.float_info.max)
        exigir(isinstance(u.get('firma'), str) and
               (bool(re.fullmatch(r'sha256:[0-9a-f]{64}', u['firma'])) if falla else u['firma'] == ''),
               'firma-invalida')
        exigir(u['resultado'] != 'no-corrida' or u['duracion_s'] == 0)
    exigir(rutas == sorted(set(rutas)), 'unidades-desordenadas-o-duplicadas')
    exigir(not filtros or rutas == list(filtros), 'seleccion-no-corresponde')
    completo = all(u['resultado'] != 'no-corrida' for u in r['unidades'])
    fallas = any(u['resultado'] == 'falla' for u in r['unidades'])
    # Enmienda 1: e2e sale 0 cuando sus fallos ya estaban en la base de main.
    exigir(r['completo'] == completo and (codigo != 0 or completo) and
           (not fallas or codigo != 0 or modo == 'e2e'), 'veredicto-incoherente')
    # Un error completo sin fallos no demuestra un resultado de producto.
    exigir(codigo != 2 and (codigo in (0, 75) or fallas or not completo), 'salida-sin-fallo')
    return r


def leer_salida(registro, log, codigo, commit, modo, filtros):
    estado = leer_json(codigo)
    if estado == 75 and leer_pendiente_boat(log) is not None:
        return None
    host = leer_json(registro / 'solicitud.json')[2]
    resultado = leer_recibo(log, estado, commit, modo, filtros, host=host)
    # Tras un reenganche la tarea original puede seguir viva: un 64 no autoriza otra clave.
    if (isinstance(resultado, RechazoLanzador) and resultado.detalle == 'lanzador-rechazo' and
            any(registro.glob('codigo-reenganche-*.json'))):
        return RechazoLanzador(resultado.evidencia, 'salida-sin-recibo')
    return resultado


class Jale:
    firma_distingue_fallo = False

    def __init__(self, lanzador, git=None, host='auto', modos=MODOS, estado=None, *,
                 resolver_lanzador=True, prelanzar=False, boat_esperar_s=ESPERA_BOAT_S,
                 repeticiones_paralelas=1):
        if not (isinstance(modos, (tuple, list)) and modos and
                all(m in MODOS for m in modos) and len(set(modos)) == len(modos)):
            raise ValueError('modos-invalidos')
        if not ((isinstance(host, str) and host in HOSTS) or
                (isinstance(host, dict) and set(modos) <= set(host) <= set(MODOS) and
                 all(isinstance(valor, str) and valor in HOSTS for valor in host.values()))):
            raise ValueError('host-invalido')
        if type(prelanzar) is not bool:
            raise ValueError('prelanzar-invalido')
        if type(boat_esperar_s) is not int or not 0 <= boat_esperar_s <= 7200:
            raise ValueError('boat-esperar-invalido')
        if type(repeticiones_paralelas) is not int or not 1 <= repeticiones_paralelas <= 8:
            raise ValueError('repeticiones-paralelas-invalidas')
        self.prelanzar_habilitado = prelanzar
        self.boat_esperar_s = boat_esperar_s
        self.repeticiones_paralelas = repeticiones_paralelas
        self.lanzador = str(Path(lanzador).resolve()) if resolver_lanzador else str(lanzador)
        self.lanzador_configurado = str(lanzador)
        self.operador = None
        self.limite_cancelacion = None
        self.cancelacion_aplaza_prelanzamiento = False
        self.git = git
        self.host, self.modos = host.copy() if isinstance(host, dict) else host, tuple(modos)
        self.estado = (Path(estado) if estado is not None else
                       directorio_estado() / 'jale').expanduser().resolve()

    def host_de(self, modo):
        return self.host[modo] if isinstance(self.host, dict) else self.host

    @property
    def clases_boat(self):
        return tuple(modo for modo in self.modos if self.host_de(modo) == 'boat')

    def prelanzar(self, candidato, tren_id):
        if not self.prelanzar_habilitado:
            return
        raiz = self.estado / clave('suite', tren_id)
        aplazado = self.archivo_prelanzamiento_aplazado(tren_id)
        if (self.cancelacion_aplaza_prelanzamiento or
                self.operador is not None and self.cancelaciones_en_curso()):
            if not aplazado.exists():
                raiz.mkdir(parents=True, exist_ok=True, mode=0o700)
                guardar(aplazado, True)
            return
        lanzadas = []
        for modo in self.modos:
            self.prelanzar_corrida(raiz / modo, candidato.C, modo, (), tren_id, lanzadas)
        self.esperar_prelanzadas(lanzadas)
        aplazado.unlink(missing_ok=True)

    def archivo_prelanzamiento_aplazado(self, tren_id):
        return self.estado / clave('suite', tren_id) / 'prelanzamiento-aplazado.json'

    def prelanzamiento_aplazado(self, tren_id):
        return (self.prelanzar_habilitado and
                self.archivo_prelanzamiento_aplazado(tren_id).exists())

    def prelanzar_corrida(self, raiz, commit, modo, filtros, tren_id, lanzadas):
        if not (raiz / 'completo.json').exists():
            registro = raiz / '0'
            proceso = self.lanzar(registro, commit, modo, filtros, True, tren_id, prelanzamiento=True)
            if proceso and proceso is not EN_CURSO:
                lanzadas.append((registro / 'candado', proceso))

    def esperar_prelanzadas(self, lanzadas):
        limite = time.monotonic() + PLAZO_PRELANZAMIENTO_S
        for candado, proceso in lanzadas:
            while (proceso.poll() is None and not lanzamiento_activo(candado)
                   and time.monotonic() < limite):
                time.sleep(.01)

    def suite_con_coordinador(self, tren_id):
        """Un coordinador de la suite pudo arrancar: lectura de disco, sin red."""
        raiz = self.estado / clave('suite', tren_id)
        return any((registro / 'iniciado.json').exists() or (registro / 'codigo.json').exists()
                   or lanzamiento_activo(registro / 'candado')
                   for registro in raiz.glob('*/*')
                   if registro.is_dir() and not (registro / CANCELAR).exists())

    def indice_adelantos(self, tren_id):
        return self.estado / clave('adelantos', tren_id)

    def adelantos_anteriores(self, tren_id, fase):
        if not isinstance(fase, Diagnosticando):
            return
        # Registros anteriores al índice. Las claves sobreviven a cambios
        # de tanda y se pueden deducir mientras se conserva el diagnóstico.
        prs = tuple(pr for pr, _, _ in fase.candidato.eslabones)
        objetivos = [('C', ()), ('B', ())]
        objetivos.extend(('subconjunto', grupo) for n in range(1, len(prs))
                         for grupo in combinations(prs, n))
        fallos = {identidad(f): f for f in fase.fallos if f.parte == 'jale' and f.check in MODOS}
        for identidad_fallo in sorted(fallos):
            fallo = fallos[identidad_fallo]
            for objetivo, grupo in objetivos:
                solicitud = id_reproduccion(fallo, objetivo, grupo)
                yield clave('repetir', tren_id, solicitud), fallo.check

    def registros_prelanzados(self, tren_id, fase):
        raiz = self.estado / clave('suite', tren_id)
        for modo in MODOS:
            yield raiz / modo / '0', modo
        adelantos = set(self.adelantos_anteriores(tren_id, fase))
        for archivo in self.indice_adelantos(tren_id).glob('*.json'):
            nombre = re.fullmatch(r'([0-9a-f]{64})\.(contract|e2e)', archivo.stem)
            if nombre is not None:
                adelantos.add(nombre.groups())
        for raiz, modo in sorted(adelantos):
            for n in ('1', '2'):
                yield self.estado / raiz / n / '0', modo

    def prelanzadas_sin_leer(self, tren_id, fase):
        for registro, modo in self.registros_prelanzados(tren_id, fase):
            if especulativa(registro):
                yield registro, {**aviso_prelanzado(registro, tren_id, modo),
                                 'cancelacion_pedida': False, 'cancelacion_confirmada': False}

    def archivo_cancelaciones(self):
        return self.estado / (clave('cancelaciones', self.operador.repo) + '.json')

    def cancelaciones_en_curso(self):
        archivo = self.archivo_cancelaciones()
        if not archivo.exists():
            return set()
        rutas = leer_json(archivo)
        exigir(isinstance(rutas, list) and all(isinstance(ruta, str) and re.fullmatch(
            r'[a-f0-9]{64}/(?:contract|e2e|1|2)/(?:0|[1-9][0-9]*)', ruta) for ruta in rutas),
            'cancelaciones-invalidas')
        return {ruta for ruta in rutas
                if lanzamiento_activo(self.estado / ruta / 'candado')}

    def cancelar_corridas(self, terminales, confirmadas):
        prelanzadas = [corrida for tren in terminales
                       for corrida in self.prelanzadas_sin_leer(tren.id, tren.fase)]
        activas = {}
        for registro, aviso in chain(prelanzadas, confirmadas):
            codigo = registro / 'codigo.json'
            pedido = registro / CANCELAR
            try:
                if pedido.exists():
                    # La marca se escribe antes de la salida. El reintento
                    # conserva el aviso y vuelve a comprobar el candado.
                    aviso['terminada'] = False
                    if (lanzamiento_activo(registro / 'candado') or codigo.exists() or
                            (registro / ACUSE_CANCELACION).exists()):
                        activas[registro] = aviso
                        aviso['cancelacion_pedida'] = True
                    else:
                        aviso['error_cancelacion'] = 'coordinador-ausente'
                    continue
                if aviso['terminada']:
                    continue
                activa = con_candado(registro)
                if codigo.exists():
                    continue
                if activa:
                    activas[registro] = aviso
                # La marca impide el arranque incluso si el coordinador
                # todavía no tomó el candado o el tick muere tras pedirlo.
                guardar(pedido, True)
                if activa or con_candado(registro):
                    activas[registro] = aviso
                    aviso['cancelacion_pedida'] = True
                elif not codigo.exists():
                    aviso['error_cancelacion'] = 'coordinador-ausente'
            except OSError as error:
                aviso['error_cancelacion'] = type(error).__name__

        # Un plazo común para todos los lanzadores. El candado se conserva
        # hasta que sale el launcher que hace la cancelación en su host.
        pendientes = set(activas)
        limite = time.monotonic() + PLAZO_CANCELACION_S
        if self.limite_cancelacion is not None:
            limite = min(limite, self.limite_cancelacion)
        while pendientes:
            pendientes = {r for r in pendientes if lanzamiento_activo(r / 'candado')}
            restante = limite - time.monotonic()
            if not pendientes or restante <= 0:
                break
            time.sleep(min(.05, restante))
        for registro, aviso in activas.items():
            aviso['cancelacion_confirmada'] = (
                aviso['cancelacion_pedida'] and registro not in pendientes)
            acuse = registro / ACUSE_CANCELACION
            if acuse.exists():
                try:
                    estado = leer_json(acuse)
                except ResultadoInvalido:
                    estado = 'acuse-invalido'
                if estado != 'senal-enviada':
                    aviso['error_cancelacion'] = estado
            elif (registro in pendientes or not (registro / 'codigo.json').exists()) and (
                    'error_cancelacion' not in aviso):
                aviso['error_cancelacion'] = 'coordinador-sin-acuse'
        if pendientes:
            self.cancelacion_aplaza_prelanzamiento = True
            try:
                anteriores = self.cancelaciones_en_curso()
                guardar(self.archivo_cancelaciones(),
                        sorted(anteriores | {str(r.relative_to(self.estado)) for r in pendientes}))
            except (OSError, ResultadoInvalido) as error:
                for registro in pendientes:
                    activas[registro]['error_barrera_cancelacion'] = type(error).__name__
        return [aviso for _, aviso in prelanzadas]

    def corridas_sin_leer(self, efecto):
        tren_id = efecto.datos[0]
        if efecto.tipo == 'repetir':
            raiz = self.estado / clave('repetir', tren_id, efecto.datos[1])
            corridas = ('1', '2')
            intento = efecto.datos[2]
        else:
            raiz = self.estado / clave('suite', tren_id)
            corridas = MODOS
            intento = efecto.datos[1] if efecto.tipo == 'reanudar-suite' else '0'
        for corrida in corridas:
            registro = raiz / corrida / intento
            if especulativa(registro) or (registro.parent / 'completo.json').exists():
                continue
            viva = lanzamiento_activo(registro / 'candado')
            terminada = (registro / 'codigo.json').exists()
            if not (viva or terminada or (registro / CANCELAR).exists()):
                continue
            modo = corrida
            if efecto.tipo == 'repetir':
                try:
                    modo = leer_json(registro / 'solicitud.json')[4]
                except (ResultadoInvalido, IndexError, KeyError, TypeError):
                    continue
                if modo not in MODOS:
                    continue
            id_corrida = clave_registro(registro)
            try:
                iniciado = leer_json(registro / 'iniciado.json')
            except ResultadoInvalido:
                iniciado = None
            try:
                entorno = leer_json(registro / 'entorno.json')
            except ResultadoInvalido:
                entorno = None
            for origen, campo in ((iniciado, 'clave'), (entorno, 'SEN_RUN_ID')):
                valor = clave_iniciada(origen, campo)
                if valor is not None:
                    id_corrida = valor
                    break
            yield registro, {'tren': tren_id, 'modo': modo, 'clave': id_corrida,
                             'terminada': terminada, 'cancelacion_pedida': False,
                             'cancelacion_confirmada': False}

    def lanzar(self, registro, commit, modo, filtros, permitir_lanzar, tren_id=None,
               reenganche=None, *, prelanzamiento=False):
        """Devuelve el coordinador, EN_CURSO, None si hay salida o False si no se autorizó."""
        sin_confirmar = especulativa(registro)
        if (registro / CANCELAR).exists():
            return
        if sin_confirmar and not permitir_lanzar:
            return False
        registro.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = registro / 'corrida.log'
        codigo = registro / 'codigo.json'
        host = self.host_de(modo)
        argv = ['bash', self.lanzador, host, str(log), modo, commit, *filtros]
        solicitud = registro / 'solicitud.json'
        candado_corrida = registro / 'candado'
        if solicitud.exists():
            guardado = leer_json(solicitud)
            exigir(isinstance(guardado, list) and len(guardado) == len(argv) and
                   guardado[0] == 'bash' and isinstance(guardado[1], str) and
                   Path(guardado[1]).is_absolute() and guardado[2] in HOSTS and
                   guardado[3:] == argv[3:], 'recibo-no-corresponde')
            if prelanzamiento and not sin_confirmar:
                return
        elif not permitir_lanzar:
            return False
        else:
            if self.operador is not None and self.operador.checkout is not None:
                argv[1] = self.operador.lanzador(self.lanzador_configurado, tren_id, codigo)
            variables = {}
            if host == 'boat':
                intento = int(registro.name)
                identidad = (identidad_boat_pendiente(registro.with_name(str(intento - 1)))
                             if intento else None)
                variables = {
                    'REMOTE_RUN_CLAVE': None,
                    'SEN_E2E_FRAGMENTO': None,
                    'SEN_RUN_OWNER': 'pao-tren',
                    'SEN_RUN_ID': identidad or clave_registro(registro),
                    'SEN_BOAT_PRIORIDAD': 'tren',
                    'SEN_BOAT_ESPERAR': str(self.boat_esperar_s),
                    'TURBO_FORCE': 'true',
                }
            guardar(registro / 'entorno.json', variables)
            if prelanzamiento:
                guardar(registro / 'prelanzada.json', True)
            guardado = argv
            guardar(solicitud, argv)
        if sin_confirmar and not prelanzamiento:
            guardar(registro / 'confirmada.json', True)
        reenganchar = reenganche and reenganche[1] == str(registro / 'coordinador.log')
        if codigo.exists() and (prelanzamiento or not reenganchar):
            return
        with candado_corrida.open('a') as candado:
            try:
                fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return EN_CURSO
            if reenganchar:
                anterior = registro / ('codigo-reenganche-' + str(reenganche[0]) + '.json')
                # El archivo conservado también marca el reenganche ya iniciado.
                if codigo.exists() and not anterior.exists():
                    resultado = leer_salida(registro, log, codigo, commit, modo, filtros)
                    if (isinstance(resultado, RechazoLanzador) and
                            resultado.detalle == 'salida-sin-recibo'):
                        if not permitir_lanzar:
                            return False
                        codigo.rename(anterior)
                if not codigo.exists():
                    if not permitir_lanzar:
                        return False
                    # Cada reenganche, también el que recupera un tick muerto, lanza con
                    # el snapshot vigente: uno viejo saldría 64 sin consultar la clave.
                    if self.operador is not None and self.operador.checkout is not None:
                        guardado[1] = self.operador.lanzador(
                            self.lanzador_configurado, tren_id, codigo)
                        guardar(solicitud, guardado)
            if codigo.exists():
                return
            iniciado = registro / 'iniciado.json'
            if iniciado.exists():
                try:
                    if clave_iniciada(leer_json(iniciado)) is None:
                        return
                except ResultadoInvalido:
                    return
            elif not permitir_lanzar:
                return False
            env = None
            if self.operador is not None:
                self.operador.validar_guardado(guardado[1])
                env = self.operador.entorno_lanzador(guardado[1])
                # El export de Boat lee el commit del repositorio del snapshot;
                # un C recién armado sólo existe en el clon del tren.
                if self.operador.checkout is not None:
                    self.operador.disponer(commit)
            # El hijo hereda el candado para que otro tick vea la corrida incluso
            # si este proceso muere antes de que el hijo ejecute Python.
            try:
                descriptor = candado.fileno()
                return subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name(
                    'ejecucion_jale.py')), str(descriptor), str(registro)],
                    start_new_session=True, pass_fds=(descriptor,),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env)
            except (OSError, subprocess.SubprocessError):
                raise ResultadoInvalido('lanzador-no-disponible') from None

    def recoger(self, registro, proceso, commit, modo, filtros):
        if proceso is False:
            return False
        exigir(not (registro / CANCELAR).exists(), 'corrida-interrumpida')
        if proceso is EN_CURSO:
            return EN_CURSO
        log = registro / 'corrida.log'
        codigo = registro / 'codigo.json'
        if proceso is not None and proceso.poll() is None and not codigo.exists():
            return EN_CURSO
        exigir(codigo.exists(), 'corrida-interrumpida')
        return leer_salida(registro, log, codigo, commit, modo, filtros)

    def lanzar_corrida(self, raiz, commit, modo, filtros, intento, permitir_lanzar, tren_id,
                       reenganche):
        cache = raiz / 'completo.json'
        if cache.exists():
            guardado = leer_json(cache)
            exigir(type(guardado) is int and guardado >= 0, 'cache-invalida')
            registro = raiz / str(guardado)
            return registro, self.lanzar(registro, commit, modo, filtros, False)
        registro = raiz / str(intento)
        return registro, self.lanzar(registro, commit, modo, filtros, permitir_lanzar,
                                     tren_id, reenganche)

    def recoger_corrida(self, registro, proceso, commit, modo, filtros):
        cache = registro.parent / 'completo.json'
        r = self.recoger(registro, proceso, commit, modo, filtros)
        if isinstance(r, dict) and r['codigo'] != 75 and not cache.exists():
            guardar(cache, int(registro.name))
        return r

    def suite(self, candidato, tren_id, intento=0, reanudar='', permitir_lanzar=True, *, reenganche=None):
        raiz = self.estado / clave('suite', tren_id)
        exigir(not reanudar or reanudar == str(raiz), 'reanudar-no-corresponde')
        lanzadas = []
        fallo_al_lanzar = None
        for modo in self.modos:
            try:
                registro, proceso = self.lanzar_corrida(raiz / modo, candidato.C, modo, (), intento,
                                                        permitir_lanzar, tren_id, reenganche)
            except ResultadoInvalido as exc:
                fallo_al_lanzar = fallo_al_lanzar or exc
                continue
            lanzadas.append((modo, registro, proceso))
        recibos = []
        for modo, registro, proceso in lanzadas:
            try:
                recibos.append(self.recoger_corrida(registro, proceso, candidato.C, modo, ()))
            except ResultadoInvalido as exc:
                recibos.append(exc)
        # Un modo sin confirmar puede seguir vivo aunque no se permita recogerlo.
        if EN_CURSO in recibos or any(
                corrida_en_curso(raiz / modo / str(intento)) for modo in self.modos):
            return EN_CURSO
        if fallo_al_lanzar is not None:
            raise fallo_al_lanzar
        rechazos = [r for r in recibos if isinstance(r, RechazoLanzador)]
        invalidos = [r for r in recibos if isinstance(r, ResultadoInvalido)]
        # Sin salida durable, el mismo intento debe poder recuperar la corrida.
        for r in invalidos:
            if r.resultado.codigo == 'corrida-interrumpida':
                raise r
        if invalidos and not rechazos:
            raise invalidos[0]
        if reenganche and any(proceso is False and
                              str(registro / 'coordinador.log') == reenganche[1]
                              for _, registro, proceso in lanzadas):
            return None
        if rechazos:
            # Prioriza el envío incierto: con reanudar conserva su identidad.
            return str(raiz), next((r for r in rechazos if r.detalle == 'salida-sin-recibo'),
                                  rechazos[0])
        if any(r is False for r in recibos):
            return None
        if any(r is None or r['codigo'] == 75 for r in recibos):
            return str(raiz), Incompleto(75, str(raiz))
        ruta = raiz / 'suite.json'
        guardar(ruta, recibos)
        fallos = tuple(Fallo(u['unidad'], u['firma'], 'solo-candidato', r['modo'],
                            r['modo'], 'jale', u['duracion_s'])
                       for r in recibos for u in r['unidades'] if u['resultado'] == 'falla')
        restantes = tuple(u['unidad'] for r in recibos for u in r['unidades']
                          if u['resultado'] == 'no-corrida')
        return str(ruta), Completo('rojo' if fallos or restantes else 'verde', fallos,
            sum(u['duracion_s'] for r in recibos for u in r['unidades']), restantes)

    def preparar_repeticion(self, candidato, suite, solicitud, commit, tren_id, reanudar='',
                            permitir_lanzar=True, *, intento=0):
        fallo = solicitud.fallo
        exigir(fallo.parte == 'jale' and fallo.check in self.modos, 'seleccion-no-corresponde')
        raiz = self.estado / clave('repetir', tren_id, solicitud.id)
        base = self.cache_de_base(candidato, commit, fallo)
        exigir(not reanudar or reanudar == str(raiz), 'reanudar-no-corresponde')
        origen = leer_json(suite)
        exigir(isinstance(origen, list) and all(isinstance(r, dict) for r in origen) and
            any(r.get('sha') == candidato.C and r.get('modo') == fallo.check and
                isinstance(r.get('unidades'), list) and any(isinstance(u, dict) and
                    u.get('unidad') == fallo.unidad for u in r['unidades'])
                for r in origen), 'origen-no-corresponde')
        if permitir_lanzar and commit not in (candidato.B, candidato.C) and not any(
                (raiz / n / str(intento) / 'solicitud.json').exists() for n in ('1', '2')):
            exigir(self.git is not None, 'subconjunto-sin-git')
            publicado = self.git.publicar('subconjunto-' + clave(tren_id, commit), commit)
            exigir(isinstance(publicado, Publicado), 'subconjunto-no-publicado')
        return raiz, base

    def cache_de_base(self, candidato, commit, fallo):
        return (self.estado / clave('base', commit, fallo.check, fallo.unidad)
                if commit == candidato.B else None)

    def adelanto(self, candidato, tren_id, solicitud):
        raiz = self.estado / clave('repetir', tren_id, solicitud.id)
        base = (self.cache_de_base(candidato, candidato.B, solicitud.fallo)
                if solicitud.objetivo == 'B' else None)
        if any(lanzamiento_activo(raiz / n / '0' / 'candado') for n in ('1', '2')):
            return 'vivo'
        return 'terminado' if all(
            (raiz / n / '0' / 'codigo.json').exists() or
            (base is not None and (base / n / 'registro.json').exists())
            for n in ('1', '2')) else ''

    def adelantar_repeticiones(self, candidato, suite, pedidos, tren_id):
        if self.repeticiones_paralelas == 1:
            return
        lanzadas = []
        try:
            for solicitud, commit in islice(pedidos, self.repeticiones_paralelas - 1):
                raiz, base = self.preparar_repeticion(candidato, suite, solicitud, commit, tren_id)
                fallo = solicitud.fallo
                indice = self.indice_adelantos(tren_id)
                indice.mkdir(parents=True, exist_ok=True, mode=0o700)
                guardar(indice / (raiz.name + '.' + fallo.check + '.json'), True)
                for n in ('1', '2'):
                    if base is None or not (base / n / 'registro.json').exists():
                        self.prelanzar_corrida(raiz / n, commit, fallo.check,
                                              (fallo.unidad,), tren_id, lanzadas)
        finally:
            self.esperar_prelanzadas(lanzadas)

    def repetir(self, candidato, suite, solicitud, commit, tren_id, intento=0, reanudar='',
                permitir_lanzar=True, *, reenganche=None):
        raiz, base = self.preparar_repeticion(candidato, suite, solicitud, commit, tren_id,
                                              reanudar, permitir_lanzar, intento=intento)
        fallo = solicitud.fallo
        filtros = (fallo.unidad,)
        fallo_al_lanzar = None
        paralelo = self.repeticiones_paralelas > 1

        def lanzar_intentos():
            nonlocal fallo_al_lanzar
            for n in (1, 2):
                cache = base / str(n) / 'registro.json' if base is not None else None
                cache_disponible = cache is not None and cache.exists()
                raiz_corrida = raiz / str(n)
                log = raiz_corrida / str(intento) / 'coordinador.log'
                # Otro tren pudo completar el caché mientras éste estaba pausado.
                desde_base = cache_disponible and not (reenganche and reenganche[1] == str(log))
                try:
                    if desde_base:
                        puntero = leer_json(cache)
                        exigir(isinstance(puntero, str) and re.fullmatch(
                            r'[0-9a-f]{64}/' + str(n) + r'/[0-9]+', puntero), 'cache-invalida')
                        registro = self.estado / puntero
                        proceso = self.lanzar(registro, commit, fallo.check, filtros, False)
                    else:
                        registro, proceso = self.lanzar_corrida(raiz_corrida, commit, fallo.check,
                            filtros, intento, permitir_lanzar, tren_id, reenganche)
                except ResultadoInvalido as exc:
                    fallo_al_lanzar = fallo_al_lanzar or exc
                    if not paralelo:
                        return
                    continue
                yield n, cache, cache_disponible, desde_base, registro, proceso
                if proceso is False:
                    return

        intentos = []
        lanzadas = lanzar_intentos()
        if paralelo:
            lanzadas = list(lanzadas)
        # Con intentos simultáneos los dos ya corren: antes de devolver el primer
        # resultado que impide la repetición se recogen todos, para que un recibo
        # válido quede en la caché y el intento siguiente no lo vuelva a lanzar.
        impedimento = SIN_IMPEDIMENTO
        en_curso = False
        for n, cache, cache_disponible, desde_base, ultimo, proceso in lanzadas:
            try:
                if desde_base:
                    r = self.recoger(ultimo, proceso, commit, fallo.check, filtros)
                    exigir(r is EN_CURSO or (isinstance(r, dict) and r['codigo'] != 75),
                           'cache-invalida')
                else:
                    r = self.recoger_corrida(ultimo, proceso, commit, fallo.check, filtros)
                    if isinstance(r, dict) and r['codigo'] != 75:
                        if cache is not None and not cache_disponible:
                            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            guardar(cache, str(ultimo.relative_to(self.estado)))
            except ResultadoInvalido as exc:
                r = exc
            if r is EN_CURSO:
                en_curso = True
                if not paralelo:
                    break
                continue
            if impedimento is not SIN_IMPEDIMENTO:
                continue
            if isinstance(r, dict) and r['codigo'] != 75:
                u = r['unidades'][0]
                intentos.append(Intento(n, 'omitida' if u['resultado'] == 'no-corrida' else
                    u['resultado'], u['firma'], u['duracion_s'], str(ultimo.parent),
                    json.dumps(r['huella_entorno'], sort_keys=True)))
                continue
            impedimento = r
            if not paralelo:
                break
        # Un impedimento puede cortar el recorrido antes de un intento ya lanzado.
        if en_curso or any(corrida_en_curso(raiz / str(n) / str(intento)) for n in (1, 2)):
            return EN_CURSO
        if fallo_al_lanzar is not None and (not paralelo or not lanzadas or lanzadas[0][0] == 2):
            raise fallo_al_lanzar
        if paralelo and any(proceso is False for *_, proceso in lanzadas):
            return None
        if isinstance(impedimento, ResultadoInvalido):
            raise impedimento
        if impedimento is False:
            return None
        if isinstance(impedimento, RechazoLanzador):
            return impedimento
        if impedimento is not SIN_IMPEDIMENTO:
            return Incompleto(75, str(raiz))
        if fallo_al_lanzar is not None:
            raise fallo_al_lanzar
        return Repeticion(commit, candidato.C, candidato.B,
            hashlib.sha256(Path(suite).read_bytes()).hexdigest(),
            (UnidadRepetida(fallo.check, fallo.unidad, fallo.parte,
                            fallo.check + ':' + fallo.unidad, tuple(intentos)),),
            tuple(leer_json(ultimo / 'solicitud.json')))
