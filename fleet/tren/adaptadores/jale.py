"""Recibos jale-recibo@1; sin juicio contra base ni normalización de firmas."""
import base64
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from adaptadores.ejecucion_jale import guardar
from adaptadores.hasito import ResultadoInvalido, exigir, leer_json
from dominio import (Completo, Fallo, Incompleto, Intento, Repeticion,
                     UnidadRepetida, clave)
from git_ops import Publicado, sha
from ledger import directorio_estado


MODOS = ('contract', 'e2e')


def objeto(pares):
    resultado = {}
    for k, v in pares:
        if k in resultado:
            raise ValueError('campo duplicado')
        resultado[k] = v
    return resultado


def leer_recibo(log, codigo, commit, modo, filtros):
    exigir(codigo != 64, 'jale-paro-64')
    exigir(type(codigo) is int and 0 <= codigo <= 255, 'salida-no-utilizable')
    try:
        lineas = Path(log).read_text(encoding='utf-8').splitlines()
    except OSError:
        lineas = []
    except UnicodeError:
        raise ResultadoInvalido('recibo-malformado') from None
    recibos = [linea for linea in lineas if linea.startswith('RECIBO')]
    if not recibos and codigo == 75:
        return None
    exigir(len(recibos) == 1, 'salida-sin-recibo')
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


class Jale:
    def __init__(self, lanzador, git=None, host='auto', modos=MODOS, estado=None):
        if not isinstance(host, str) or host not in ('auto', 'mini', 'desktop'):
            raise ValueError('host-invalido')
        if not (isinstance(modos, (tuple, list)) and modos and
                all(m in MODOS for m in modos) and len(set(modos)) == len(modos)):
            raise ValueError('modos-invalidos')
        self.lanzador = str(Path(lanzador).resolve())
        self.git = git
        self.operador = None
        self.host, self.modos = host, tuple(modos)
        self.estado = (Path(estado) if estado is not None else
                       directorio_estado() / 'jale').expanduser().resolve()

    def esperar(self, proceso):
        proceso.wait()

    def correr(self, registro, commit, modo, filtros, permitir_lanzar):
        registro.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = registro / 'corrida.log'
        argv = ['bash', self.lanzador, self.host, str(log), modo, commit, *filtros]
        solicitud = registro / 'solicitud.json'
        if solicitud.exists():
            exigir(leer_json(solicitud) == argv, 'recibo-no-corresponde')
        elif not permitir_lanzar:
            return False
        else:
            guardar(solicitud, argv)
        if (not permitir_lanzar and not (registro / 'iniciado.json').exists() and
                not (registro / 'codigo.json').exists()):
            return False
        if not (registro / 'codigo.json').exists():
            if self.operador is not None:
                self.operador.avanzar()
            # Otro coordinador espera el mismo candado y recupera su resultado.
            # La sesión nueva sigue viva aunque muera el proceso del tick.
            try:
                proceso = subprocess.Popen([sys.executable, str(Path(__file__).with_name(
                    'ejecucion_jale.py')), str(registro)], start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.esperar(proceso)
            except (OSError, subprocess.SubprocessError):
                raise ResultadoInvalido('lanzador-no-disponible') from None
        exigir((registro / 'codigo.json').exists(), 'corrida-interrumpida')
        return leer_recibo(log, leer_json(registro / 'codigo.json'), commit, modo, filtros)

    def corrida(self, raiz, commit, modo, filtros, intento, permitir_lanzar):
        cache = raiz / 'completo.json'
        if cache.exists():
            guardado = leer_json(cache)
            exigir(type(guardado) is int and guardado >= 0, 'cache-invalida')
            return self.correr(raiz / str(guardado), commit, modo, filtros, False)
        r = self.correr(raiz / str(intento), commit, modo, filtros, permitir_lanzar)
        if isinstance(r, dict) and r['codigo'] != 75:
            guardar(cache, intento)
        return r

    def suite(self, candidato, tren_id, intento=0, reanudar='', permitir_lanzar=True):
        raiz = self.estado / clave('suite', tren_id)
        exigir(not reanudar or reanudar == str(raiz), 'reanudar-no-corresponde')
        recibos = []
        for modo in self.modos:
            r = self.corrida(raiz / modo, candidato.C, modo, (), intento, permitir_lanzar)
            if r is False:
                return None
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
                permitir_lanzar=True):
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
            if cache is not None and cache.exists():
                puntero = leer_json(cache)
                exigir(isinstance(puntero, str) and re.fullmatch(
                    r'[0-9a-f]{64}/' + str(n) + r'/[0-9]+', puntero), 'cache-invalida')
                ultimo = self.estado / puntero
                r = self.correr(ultimo, commit, fallo.check, (fallo.unidad,), False)
                exigir(isinstance(r, dict) and r['codigo'] != 75, 'cache-invalida')
            else:
                r = self.corrida(raiz / str(n), commit, fallo.check, (fallo.unidad,), intento,
                                 permitir_lanzar)
                if isinstance(r, dict) and r['codigo'] != 75:
                    ultimo = raiz / str(n) / str(leer_json(raiz / str(n) / 'completo.json'))
                    if cache is not None:
                        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        guardar(cache, str(ultimo.relative_to(self.estado)))
            if r is False:
                return None
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
