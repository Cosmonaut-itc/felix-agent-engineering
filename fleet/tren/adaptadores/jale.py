"""Recibos jale-recibo@1; sin juicio contra base ni normalización de firmas."""
import base64
import binascii
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

from adaptadores.ejecucion_jale import guardar
from adaptadores.hasito import ResultadoInvalido, exigir, leer_json
from dominio import (Completo, Fallo, Incompleto, Intento, Repeticion,
                     UnidadRepetida, RechazoLanzador, clave)
from git_ops import Publicado, sha
from ledger import directorio_estado
from operador import lanzamiento_activo


MODOS = ('contract', 'e2e')
PLAZO_PRELANZAMIENTO_S = 5


def especulativa(registro):
    return ((registro / 'prelanzada.json').exists() and
            not (registro / 'confirmada.json').exists())


def objeto(pares):
    resultado = {}
    for k, v in pares:
        if k in resultado:
            raise ValueError('campo duplicado')
        resultado[k] = v
    return resultado


def leer_recibo(log, codigo, commit, modo, filtros):
    exigir(type(codigo) is int, 'salida-no-utilizable')
    if codigo == 64:
        return RechazoLanzador(str(Path(log).with_name('coordinador.log')))
    try:
        lineas = Path(log).read_text(encoding='utf-8').splitlines()
    except OSError:
        lineas = []
    except UnicodeError:
        raise ResultadoInvalido('recibo-malformado') from None
    recibos = [linea for linea in lineas if linea.startswith('RECIBO')]
    if not recibos:
        if codigo == 75:
            return None
        detalle = ('corrida-interrumpida' if codigo == 125 and 'PUEUE_INTERRUPTED=1' in lineas
                   else 'salida-sin-recibo')
        return RechazoLanzador(str(Path(log).with_name('coordinador.log')), detalle)
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
    resultado = leer_recibo(log, leer_json(codigo), commit, modo, filtros)
    # Tras un reenganche la tarea original puede seguir viva: un 64 no autoriza otra clave.
    if (isinstance(resultado, RechazoLanzador) and resultado.detalle == 'lanzador-rechazo' and
            any(registro.glob('codigo-reenganche-*.json'))):
        return RechazoLanzador(resultado.evidencia, 'salida-sin-recibo')
    return resultado


class Jale:
    firma_distingue_fallo = False

    def __init__(self, lanzador, git=None, host='auto', modos=MODOS, estado=None, *,
                 resolver_lanzador=True, prelanzar=False):
        if not isinstance(host, str) or host not in ('auto', 'mini', 'desktop'):
            raise ValueError('host-invalido')
        if not (isinstance(modos, (tuple, list)) and modos and
                all(m in MODOS for m in modos) and len(set(modos)) == len(modos)):
            raise ValueError('modos-invalidos')
        if type(prelanzar) is not bool:
            raise ValueError('prelanzar-invalido')
        self.prelanzar_habilitado = prelanzar
        self.lanzador = str(Path(lanzador).resolve()) if resolver_lanzador else str(lanzador)
        self.lanzador_configurado = str(lanzador)
        self.operador = None
        self.git = git
        self.host, self.modos = host, tuple(modos)
        self.estado = (Path(estado) if estado is not None else
                       directorio_estado() / 'jale').expanduser().resolve()

    def esperar(self, proceso):
        proceso.wait()

    def prelanzar(self, candidato, tren_id):
        if self.prelanzar_habilitado:
            modo = self.modos[0]
            raiz = self.estado / clave('suite', tren_id) / modo
            if not (raiz / 'completo.json').exists():
                self.correr(raiz / '0', candidato.C, modo, (), True, tren_id,
                            prelanzamiento=True)

    def prelanzada_sin_leer(self, tren_id):
        raiz = self.estado / clave('suite', tren_id)
        for modo in MODOS:
            registro = raiz / modo / '0'
            if especulativa(registro):
                try:
                    iniciado = leer_json(registro / 'iniciado.json')
                except ResultadoInvalido:
                    iniciado = None
                id_corrida = iniciado.get('clave') if isinstance(iniciado, dict) else None
                if not isinstance(id_corrida, str) or not re.fullmatch(
                        r'[A-Za-z0-9._-]{8,128}', id_corrida):
                    id_corrida = None
                return {'tren': tren_id, 'clave': id_corrida,
                        'terminada': (registro / 'codigo.json').exists()}
        return None

    def correr(self, registro, commit, modo, filtros, permitir_lanzar, tren_id=None,
               reenganche=None, *, prelanzamiento=False):
        sin_confirmar = especulativa(registro)
        if sin_confirmar and not permitir_lanzar:
            return False
        registro.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = registro / 'corrida.log'
        codigo = registro / 'codigo.json'
        argv = ['bash', self.lanzador, self.host, str(log), modo, commit, *filtros]
        solicitud = registro / 'solicitud.json'
        candado_corrida = registro / 'candado'
        if solicitud.exists():
            guardado = leer_json(solicitud)
            exigir(isinstance(guardado, list) and len(guardado) == len(argv) and
                   guardado[0] == 'bash' and isinstance(guardado[1], str) and
                   Path(guardado[1]).is_absolute() and guardado[2] in ('auto', 'mini', 'desktop') and
                   guardado[3:] == argv[3:], 'recibo-no-corresponde')
            if prelanzamiento and not sin_confirmar:
                return
        elif not permitir_lanzar:
            return False
        else:
            if self.operador is not None and self.operador.checkout is not None:
                argv[1] = self.operador.lanzador(self.lanzador_configurado, tren_id, codigo)
            if prelanzamiento:
                guardar(registro / 'prelanzada.json', True)
            guardado = argv
            guardar(solicitud, argv)
        if sin_confirmar and not prelanzamiento:
            guardar(registro / 'confirmada.json', True)
        if prelanzamiento and (codigo.exists() or lanzamiento_activo(candado_corrida)):
            return
        if reenganche and reenganche[1] == str(registro / 'coordinador.log'):
            anterior = registro / ('codigo-reenganche-' + str(reenganche[0]) + '.json')
            with (registro / 'candado').open('a') as candado:
                fcntl.flock(candado, fcntl.LOCK_EX)
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
        if not codigo.exists():
            if not permitir_lanzar and not (registro / 'iniciado.json').exists():
                return False
            env = None
            if self.operador is not None:
                self.operador.validar_guardado(guardado[1])
                env = self.operador.entorno_lanzador(guardado[1])
            # Otro coordinador espera el mismo candado y recupera su resultado.
            # La sesión nueva sigue viva aunque muera el proceso del tick.
            try:
                proceso = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name(
                    'ejecucion_jale.py')), str(registro)], start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env)
                if prelanzamiento:
                    limite = time.monotonic() + PLAZO_PRELANZAMIENTO_S
                    while (proceso.poll() is None and not lanzamiento_activo(candado_corrida)
                           and time.monotonic() < limite):
                        time.sleep(.01)
                    return
                self.esperar(proceso)
            except (OSError, subprocess.SubprocessError):
                raise ResultadoInvalido('lanzador-no-disponible') from None
        exigir(codigo.exists(), 'corrida-interrumpida')
        return leer_salida(registro, log, codigo, commit, modo, filtros)

    def corrida(self, raiz, commit, modo, filtros, intento, permitir_lanzar, tren_id, reenganche=None):
        cache = raiz / 'completo.json'
        if cache.exists():
            guardado = leer_json(cache)
            exigir(type(guardado) is int and guardado >= 0, 'cache-invalida')
            return self.correr(raiz / str(guardado), commit, modo, filtros, False)
        r = self.correr(raiz / str(intento), commit, modo, filtros, permitir_lanzar, tren_id, reenganche)
        if isinstance(r, dict) and r['codigo'] != 75:
            guardar(cache, intento)
        return r

    def suite(self, candidato, tren_id, intento=0, reanudar='', permitir_lanzar=True, *, reenganche=None):
        raiz = self.estado / clave('suite', tren_id)
        exigir(not reanudar or reanudar == str(raiz), 'reanudar-no-corresponde')
        recibos = []
        for modo in self.modos:
            r = self.corrida(raiz / modo, candidato.C, modo, (), intento, permitir_lanzar,
                             tren_id, reenganche)
            if r is False:
                return None
            if isinstance(r, RechazoLanzador):
                return str(raiz), r
            if r is None or r['codigo'] == 75:
                return str(raiz), Incompleto(75, str(raiz))
            recibos.append(r)
        ruta = raiz / 'suite.json'
        guardar(ruta, recibos)
        fallos = tuple(Fallo(u['unidad'], u['firma'], 'solo-candidato', r['modo'],
                            r['modo'], 'jale', u['duracion_s'])
                       for r in recibos for u in r['unidades'] if u['resultado'] == 'falla')
        restantes = tuple(u['unidad'] for r in recibos for u in r['unidades']
                          if u['resultado'] == 'no-corrida')
        return str(ruta), Completo('rojo' if fallos or restantes else 'verde', fallos,
            sum(u['duracion_s'] for r in recibos for u in r['unidades']), restantes)

    def repetir(self, candidato, suite, solicitud, commit, tren_id, intento=0, reanudar='',
                permitir_lanzar=True, *, reenganche=None):
        fallo = solicitud.fallo
        exigir(fallo.parte == 'jale' and fallo.check in self.modos, 'seleccion-no-corresponde')
        raiz = self.estado / clave('repetir', tren_id, solicitud.id)
        base = (self.estado / clave('base', commit, fallo.check, fallo.unidad)
                if commit == candidato.B else None)
        exigir(not reanudar or reanudar == str(raiz), 'reanudar-no-corresponde')
        origen = leer_json(suite)
        exigir(isinstance(origen, list) and all(isinstance(r, dict) for r in origen) and
            any(r.get('sha') == candidato.C and r.get('modo') == fallo.check and
                isinstance(r.get('unidades'), list) and any(isinstance(u, dict) and
                    u.get('unidad') == fallo.unidad for u in r['unidades'])
                for r in origen), 'origen-no-corresponde')
        if permitir_lanzar and commit not in (candidato.B, candidato.C):
            exigir(self.git is not None, 'subconjunto-sin-git')
            publicado = self.git.publicar('subconjunto-' + clave(tren_id, commit), commit)
            exigir(isinstance(publicado, Publicado), 'subconjunto-no-publicado')
        intentos = []
        for n in (1, 2):
            cache = base / str(n) / 'registro.json' if base is not None else None
            cache_disponible = cache is not None and cache.exists()
            raiz_corrida = raiz / str(n)
            log = raiz_corrida / str(intento) / 'coordinador.log'
            # Otro tren pudo completar el caché mientras éste estaba pausado.
            if cache_disponible and not (reenganche and reenganche[1] == str(log)):
                puntero = leer_json(cache)
                exigir(isinstance(puntero, str) and re.fullmatch(
                    r'[0-9a-f]{64}/' + str(n) + r'/[0-9]+', puntero), 'cache-invalida')
                ultimo = self.estado / puntero
                r = self.correr(ultimo, commit, fallo.check, (fallo.unidad,), False)
                exigir(isinstance(r, dict) and r['codigo'] != 75, 'cache-invalida')
            else:
                r = self.corrida(raiz_corrida, commit, fallo.check, (fallo.unidad,), intento,
                                 permitir_lanzar, tren_id, reenganche)
                if isinstance(r, dict) and r['codigo'] != 75:
                    ultimo = raiz_corrida / str(leer_json(raiz_corrida / 'completo.json'))
                    if cache is not None and not cache_disponible:
                        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        guardar(cache, str(ultimo.relative_to(self.estado)))
            if r is False:
                return None
            if isinstance(r, RechazoLanzador):
                return r
            if r is None or r['codigo'] == 75:
                return Incompleto(75, str(raiz))
            u = r['unidades'][0]
            intentos.append(Intento(n, 'omitida' if u['resultado'] == 'no-corrida' else
                u['resultado'], u['firma'], u['duracion_s'], str(ultimo.parent),
                json.dumps(r['huella_entorno'], sort_keys=True)))
        return Repeticion(commit, candidato.C, candidato.B,
            hashlib.sha256(Path(suite).read_bytes()).hexdigest(),
            (UnidadRepetida(fallo.check, fallo.unidad, fallo.parte,
                            fallo.check + ':' + fallo.unidad, tuple(intentos)),),
            tuple(leer_json(ultimo / 'solicitud.json')))
