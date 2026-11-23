"""U9: contrato por fixtures; ejecutar sólo en el host de pruebas."""
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from test_tren import Aislado, cli, estado, observacion, REPO
from test_hasito import B, C, CANDIDATO
from adaptadores.hasito import Hasito, ResultadoInvalido
from dominio import (Completo, Efecto, EsperandoCI, Evidencia, Fallo, Incompleto,
                     Intento, Repeticion, Reproduccion, Tren, clave, datos, desde_datos)
from efectos import EfectoFallido, ejecutar
from atribucion import planificar
from git_ops import ErrorGit, Publicado, RamaOcupada

UNIDAD = 'apps/api/src/a.contract.test.ts'
OTRA = 'apps/api/src/b.contract.test.ts'
FIRMA = 'sha256:' + 'a' * 64
FALLO = Fallo(UNIDAD, FIRMA, 'solo-candidato', 'contract', 'contract', 'jale', 4)

# Escribe exclusivamente fixtures del contrato; no depende del productor real.
PRODUCTOR = r'''
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
raiz = Path(sys.argv[1])
host, log, modo, sha, *filtros = sys.argv[2:]
contador = raiz / 'lanzamientos.jsonl'
invocaciones = raiz / 'invocaciones.jsonl'
with (raiz / 'candado-productor').open('a') as candado:
    fcntl.flock(candado, fcntl.LOCK_EX)
    previas = invocaciones.read_text().splitlines() if invocaciones.exists() else []
    clave = os.environ.get('REMOTE_RUN_CLAVE', '')
    with invocaciones.open('a') as archivo:
        archivo.write(json.dumps(dict(clave=clave, log=log, pgid=os.getpgrp())) + '\n')
    registros = raiz / 'por-clave'
    registros.mkdir(exist_ok=True)
    identidad = clave or 'sin-clave-' + str(len(previas))
    registro = registros / (hashlib.sha256(identidad.encode()).hexdigest() + '.json')
    conocido = registro.exists()
    if conocido:
        trabajo = json.loads(registro.read_text())
        print('PUEUE_REENGANCHE=' + str(trabajo['id']), flush=True)
    else:
        previos = contador.read_text().splitlines() if contador.exists() else []
        plan = json.loads((raiz / 'plan.json').read_text())
        trabajo = dict(id=len(previos), conexiones=0, plan=plan[min(len(previos), len(plan) - 1)])
        with contador.open('a') as archivo:
            archivo.write(json.dumps([host, log, modo, sha, *filtros]) + '\n')
    trabajo['conexiones'] += 1
    registro.write_text(json.dumps(trabajo))
f = trabajo['plan']
if trabajo['conexiones'] <= f.get('transportes', 0):
    codigo = f.get('codigo_transporte', 255)
    if codigo == 127:
        if not conocido:
            print('PUEUE_ID=' + str(trabajo['id']), flush=True)
        print('Wsl/Service/WSAETIMEDOUT', file=sys.stderr, flush=True)
    Path(log).write_text('')
    sys.exit(codigo)
if f.get('esperar'):
    (raiz / 'listo').write_text('listo')
    limite = time.monotonic() + 20
    while not (raiz / 'seguir').exists() and time.monotonic() < limite:
        time.sleep(.02)
rutas = filtros or (["apps/api/src/a.contract.test.ts"] if modo == 'contract'
                     else ['apps/web/e2e/a.spec.ts'])
estados = f.get('estados', ['pasa'] * len(rutas))
if len(estados) > len(rutas):
    rutas.append('apps/api/src/b.contract.test.ts')
unidades = [dict(unidad=u, resultado=e, firma='sha256:' + 'a' * 64 if e == 'falla' else '',
                 fallidas=int(e == 'falla'), duracion_s=0 if e == 'no-corrida' else 4)
            for u, e in zip(rutas, estados)]
codigo = f.get('codigo', int('falla' in estados or 'no-corrida' in estados))
r = dict(esquema='jale-recibo@1', sha=sha, modo=modo,
         alcance='parcial' if filtros else 'completo', filtros=filtros, codigo=codigo,
         completo='no-corrida' not in estados,
         huella_entorno={'bun': '1.3.3', 'node': '22.x', 'playwright': '1.57.0'}, unidades=unidades)
r.update(f.get('cambiar', {}))
if f.get('duplicar_unidad'):
    r['unidades'] *= 2
if f.get('invertir'):
    r['unidades'].reverse()
linea = 'RECIBO jale-recibo@1 ' + base64.b64encode(json.dumps(r).encode()).decode() + '\n'
if f.get('malformado'):
    linea = 'RECIBO jale-recibo@1 xxx\n'
if f.get('duplicar_recibo'):
    linea *= 2
if f.get('sin_linea'):
    linea = ''
if f.get('interrumpida'):
    linea += 'PUEUE_INTERRUPTED=1\n'
Path(log).write_text(linea)
sys.exit(codigo)
'''


class JaleTests(Aislado):
    def setUp(self):
        super().setUp()
        # La ausencia de la implementación es un fallo explícito por caso en la base.
        try:
            modulo = importlib.import_module('adaptadores.jale')
        except ImportError:
            self.fail('U9: la base no tiene el adaptador Jale')
        self.Jale = modulo.Jale
        self.productor = self.raiz / 'productor.py'
        self.productor.write_text(PRODUCTOR)
        self.lanzador = self.raiz / 'run-remote.sh'
        self.lanzador.write_text('#!/bin/bash\nexec ' + shlex.join(
            [sys.executable, str(self.productor), str(self.raiz)]) + ' "$@"\n')
        self.plan([{}])
        self.adaptador = self.Jale(self.lanzador)

    def plan(self, filas):
        (self.raiz / 'plan.json').write_text(json.dumps(filas))

    def lanzamientos(self):
        ruta = self.raiz / 'lanzamientos.jsonl'
        return [json.loads(l) for l in ruta.read_text().splitlines()] if ruta.exists() else []

    def invocaciones(self):
        ruta = self.raiz / 'invocaciones.jsonl'
        return [json.loads(l) for l in ruta.read_text().splitlines()] if ruta.exists() else []

    def suite(self, tren='lote', **kwargs):
        return self.adaptador.suite(CANDIDATO, tren, **kwargs)

    def origen(self):
        self.plan([{'estados': ['falla']}, {}])
        return self.suite()[0]

    def repetir(self, origen, objetivo='C', **kwargs):
        return self.adaptador.repetir(CANDIDATO, origen,
            Reproduccion('solicitud-' + objetivo, FALLO, objetivo),
            B if objetivo == 'B' else C, 'lote', **kwargs)

    def invalido(self, fila, codigo=None):
        self.plan([fila])
        with self.assertRaises(ResultadoInvalido) as contexto:
            self.suite()
        if codigo:
            self.assertEqual(contexto.exception.resultado.codigo, codigo)
        self.assertEqual(len(self.lanzamientos()), 1)

    def configurar_rechazo(self, respuesta, *, interrumpida=False):
        filas = {
            '64': {'codigo': 64, 'sin_linea': True},
            '75': {'codigo': 75, 'sin_linea': True},
            '127': {'transportes': 3, 'codigo_transporte': 127},
            '125': {'codigo': 125, 'sin_linea': True, 'interrumpida': interrumpida},
            'rojo': {'estados': ['falla']},
        }
        self.plan([{}] * len(self.lanzamientos()) + [filas.get(respuesta, {})])

    def verificar_rechazo(self, tipo, **kwargs):
        from test_hasito import GitFalso, verificar_rechazo
        self.git = GitFalso(self.raiz)
        verificar_rechazo(self, tipo, self.configurar_rechazo, **kwargs)

    def test_sen7030_iniciar(self):
        self.verificar_rechazo("iniciar-suite")

    def test_sen7030_reanudar(self):
        self.verificar_rechazo("reanudar-suite")

    def test_sen7030_repetir(self):
        self.verificar_rechazo("repetir")

    def test_sen7030_recuperar(self):
        self.verificar_rechazo("iniciar-suite", recuperacion=True)

    def test_sen7030_abortar(self):
        self.verificar_rechazo("iniciar-suite", decision="abortar")

    def test_sen7030_recuperar_reanudar(self):
        self.verificar_rechazo("reanudar-suite", recuperacion=True)

    def test_sen7030_recuperar_repetir(self):
        self.verificar_rechazo("repetir", recuperacion=True)

    def test_sen7124_recuperar_repeticion_fallida_y_reanudar(self):
        self.verificar_rechazo('repetir', codigo=127, recuperacion=True)
        rechazados = [log for _, log, *_ in self.lanzamientos()
                      if Path(log).with_name('codigo-reenganche-1.json').is_file()]
        self.assertEqual(len(rechazados), 1)
        invocaciones = [i for i in self.invocaciones() if i['log'] == rechazados[0]]
        self.assertEqual(len(invocaciones), 4)
        self.assertEqual(len({i['clave'] for i in invocaciones}), 1)

    def test_sen7124_pausar_suite_sin_recibo_y_abortar(self):
        self.verificar_rechazo('iniciar-suite', codigo=127, decision='abortar')

    def test_sen7279_reanudar_suite_sin_recibo_en_intento_cero(self):
        self.verificar_rechazo('iniciar-suite', codigo=127)

    def test_sen7279_reanudar_suite_sin_recibo_despues_de_75(self):
        self.verificar_rechazo('reanudar-suite', codigo=127)

    def test_sen7279_125_interrumpido_reanuda_con_otra_clave_y_certifica(self):
        from test_hasito import GitFalso, verificar_rechazo
        self.git = GitFalso(self.raiz)
        verificar_rechazo(self, 'iniciar-suite',
            lambda respuesta: self.configurar_rechazo(respuesta, interrumpida=True),
            codigo=125, detalle='corrida-interrumpida')
        contratos = [i for i in self.invocaciones() if '/contract/' in i['log']]
        self.assertEqual([Path(i['log']).parent.name for i in contratos], ['0', '1'])
        self.assertEqual(len({i['clave'] for i in contratos}), 2)

    def test_sen7279_125_sin_marcador_pausa_por_salida_sin_recibo(self):
        self.verificar_rechazo('iniciar-suite', codigo=125, decision='abortar')

    def test_suite_verde_dos_modos_y_argv_sin_filtros(self):
        ruta, r = self.suite()
        self.assertEqual(r, Completo('verde', costo_s=8))
        self.assertTrue(Path(ruta).is_file())
        self.assertEqual([(a[0], a[2:]) for a in self.lanzamientos()],
                         [('auto', ['contract', C]), ('auto', ['e2e', C])])
        self.assertEqual(self.suite(), (ruta, r))
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_suite_roja_firma_y_no_corrida_sin_preexistentes(self):
        self.plan([{'estados': ['falla', 'no-corrida']}, {}])
        _, r = self.suite()
        self.assertEqual((r.veredicto, r.fallos, r.restantes), ('rojo', (FALLO,), (OTRA,)))

    def test_75_sin_recibo_reanuda_sin_repetir_modo_terminado(self):
        self.plan([{}, {'codigo': 75, 'sin_linea': True}, {}])
        _, r = self.suite()
        self.assertIsInstance(r, Incompleto)
        _, final = self.suite(intento=1, reanudar=r.reanudar)
        self.assertEqual(final.veredicto, 'verde')
        self.assertEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e', 'e2e'])

    def test_75_con_recibo_no_se_usa_como_fallo(self):
        self.plan([{'codigo': 75, 'estados': ['falla']}])
        self.assertIsInstance(self.suite()[1], Incompleto)

    def test_paro_64_no_reintenta_ni_cambia_host(self):
        from dominio import RechazoLanzador
        self.plan([{'codigo': 64, 'sin_linea': True}])
        recibo = self.suite()
        evidencia = str(Path(self.lanzamientos()[0][1]).with_name('coordinador.log'))
        self.assertEqual(recibo[1], RechazoLanzador(evidencia, 'lanzador-rechazo'))
        self.assertEqual(desde_datos({'_tipo': 'RechazoLanzador', 'evidencia': evidencia}),
                         recibo[1])
        self.assertEqual(self.suite(), recibo)
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_sha_ajeno(self):
        self.invalido({'cambiar': {'sha': B}}, 'sha-no-corresponde')

    def test_modo_ajeno(self):
        self.invalido({'cambiar': {'modo': 'e2e'}}, 'modo-no-corresponde')

    def test_esquema_ajeno(self):
        self.invalido({'cambiar': {'esquema': 'otro@1'}}, 'esquema-desconocido')

    def test_alcance_ajeno(self):
        self.invalido({'cambiar': {'alcance': 'parcial'}}, 'seleccion-no-corresponde')

    def test_filtros_ajenos(self):
        self.invalido({'cambiar': {'filtros': [UNIDAD]}}, 'seleccion-no-corresponde')

    def test_codigo_no_corresponde(self):
        self.invalido({'cambiar': {'codigo': 1}}, 'salida-no-corresponde')

    def test_cero_con_falla_es_infraestructura(self):
        self.invalido({'estados': ['falla'], 'codigo': 0}, 'veredicto-incoherente')

    def test_e2e_cero_con_fallos_compartidos_es_rojo_para_atribuir(self):
        self.plan([{}, {'estados': ['falla'], 'codigo': 0}])
        _, r = self.suite()
        self.assertEqual((r.veredicto, r.fallos), ('rojo', (Fallo(
            'apps/web/e2e/a.spec.ts', FIRMA, 'solo-candidato', 'e2e', 'e2e', 'jale', 4),)))

    def test_cero_incompleto_es_infraestructura(self):
        self.invalido({'estados': ['pasa', 'no-corrida'], 'codigo': 0},
                      'veredicto-incoherente')

    def test_completo_incoherente(self):
        self.invalido({'estados': ['falla', 'no-corrida'], 'cambiar': {'completo': True}},
                      'veredicto-incoherente')

    def test_unidad_duplicada(self):
        self.invalido({'duplicar_unidad': True}, 'unidades-desordenadas-o-duplicadas')

    def test_unidades_desordenadas(self):
        self.invalido({'estados': ['falla', 'pasa'], 'invertir': True},
                      'unidades-desordenadas-o-duplicadas')

    def test_sin_linea_con_salida_cero(self):
        from dominio import RechazoLanzador
        self.plan([{'sin_linea': True}])
        _, resultado = self.suite()
        self.assertEqual(len(self.lanzamientos()), 1)
        evidencia = str(Path(self.lanzamientos()[0][1]).with_name('coordinador.log'))
        self.assertEqual(resultado, RechazoLanzador(evidencia, 'salida-sin-recibo'))

    def test_base64_malformado(self):
        self.invalido({'malformado': True}, 'recibo-malformado')

    def test_recibo_duplicado(self):
        self.invalido({'duplicar_recibo': True}, 'salida-sin-recibo')

    def test_firma_sin_hash_es_infraestructura(self):
        self.invalido({'codigo': 1, 'cambiar': {'unidades': [dict(unidad=UNIDAD,
            resultado='falla', firma='error privado', fallidas=1, duracion_s=1)]}}, 'firma-invalida')

    def test_salida_uso_no_es_rojo(self):
        self.invalido({'codigo': 2}, 'salida-sin-fallo')

    def test_campos_y_tipos_invalidos_nunca_dan_verde(self):
        casos = [
            {'unidades': []},
            {'completo': 1},
            {'huella_entorno': {}},
            {'huella_entorno': {'bun': '1'}},
            {'unidades': [dict(unidad='../a.contract.test.ts', resultado='pasa',
                              firma='', fallidas=0, duracion_s=1)]},
            {'unidades': [dict(unidad=UNIDAD, resultado='pasa',
                              firma='', fallidas=True, duracion_s=1)]},
            {'unidades': [dict(unidad=UNIDAD, resultado='pasa',
                              firma='', fallidas=0, duracion_s=-1)]},
            {'unidades': [dict(unidad=UNIDAD, resultado='pasa',
                              firma='', fallidas=0, duracion_s=10 ** 400)]},
        ]
        for n, cambiar in enumerate(casos):
            with self.subTest(n=n):
                self.plan([{'cambiar': cambiar}])
                with self.assertRaises(ResultadoInvalido):
                    self.suite(tren='invalido-' + str(n))

    def test_repeticion_dos_intentos_ruta_exacta_y_huella_durable(self):
        origen = self.origen()
        self.plan([{'estados': ['falla']}])
        r = self.repetir(origen)
        self.assertIsInstance(r, Repeticion)
        self.assertEqual(r, desde_datos(datos(r)))
        u = r.unidades[0]
        self.assertEqual((u.check, u.parte, u.unidad), ('contract', 'jale', UNIDAD))
        self.assertEqual([i.n for i in u.intentos], [1, 2])
        self.assertTrue(all(i.veredicto == 'falla' and i.firma == FIRMA and
            i.duracion_s == 4 and json.loads(i.huella_entorno)['bun'] == '1.3.3'
            for i in u.intentos))
        self.assertEqual([a[2:] for a in self.lanzamientos()[2:]],
                         [['contract', C, UNIDAD]] * 2)

    def test_repeticion_omitida_no_acredita_pase(self):
        origen = self.origen()
        self.plan([{'estados': ['no-corrida']}])
        r = self.repetir(origen)
        self.assertEqual([i.veredicto for i in r.unidades[0].intentos], ['omitida', 'omitida'])

    def test_cache_base_reutilizada_en_otro_tren_y_candidato(self):
        origen = self.origen()
        self.plan([{}])
        r = self.repetir(origen, 'B')
        # Misma B, otro candidato y otro origen; reencuadra sólo el sobre.
        nuevo = replace(CANDIDATO, C='e' * 40)
        ruta, _ = self.adaptador.suite(nuevo, 'otro')
        antes = len(self.lanzamientos())
        r2 = self.Jale(self.lanzador).repetir(nuevo, ruta,
            Reproduccion('otra', FALLO, 'B'), B, 'otro', permitir_lanzar=False)
        self.assertEqual(len(self.lanzamientos()), antes)
        self.assertEqual(r2.unidades, r.unidades)
        self.assertEqual(r2.origen_sha, nuevo.C)
        self.assertNotEqual(r2.origen_sha256, r.origen_sha256)

    def repeticion_valida(self, candidato, origen, solicitud, commit, tren, **kwargs):
        try:
            r = self.adaptador.repetir(candidato, origen, solicitud, commit, tren, **kwargs)
        except ResultadoInvalido as exc:
            self.fail('se esperaba Repeticion, recibido ' + exc.resultado.codigo)
        self.assertIsInstance(r, Repeticion)
        self.assertEqual([i.veredicto for i in r.unidades[0].intentos], ['pasa', 'pasa'])
        return r

    def test_r1_subconjunto_publicado_fetch_real_e_idempotente(self):
        from test_aterrizaje import AterrizajeTests
        from dominio import Candidato
        f = AterrizajeTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        miembros = []
        for pr, fecha in ((4, '2001-01-01T00:00:00+0000'),
                          (5, '2001-01-02T00:00:00+0000')):
            with patch.dict(os.environ, {'GIT_AUTHOR_DATE': fecha, 'GIT_COMMITTER_DATE': fecha}):
                head = f.commit({'comun': 'base\n', 'pr-' + str(pr): 'ticket\n'}, (f.B,))
            f.push(head, 'refs/pull/' + str(pr) + '/head')
            miembros.append((pr, head))
        c = f.git.armar(f.B, miembros)
        self.assertIsInstance(c, Candidato)
        self.assertIsInstance(f.git.publicar('lote-r1', c.C), Publicado)
        sub = f.git.armar(f.B, miembros[1:])
        self.assertIsInstance(sub, Candidato)
        ancestro = subprocess.run(['git', '-C', str(f.git.ruta), 'merge-base',
                                  '--is-ancestor', sub.C, c.C], capture_output=True)
        self.assertEqual(ancestro.returncode, 1)
        ausente = subprocess.run(['git', '-C', str(f.origen), 'cat-file', '-e', sub.C],
                                capture_output=True)
        self.assertNotEqual(ausente.returncode, 0)
        host = f.raiz / 'host.git'
        subprocess.run(['git', 'init', '--bare', str(host)], capture_output=True, check=True)
        f.comando(host, 'remote', 'add', 'origin', str(f.origen))
        self.lanzador.write_text('#!/bin/bash\n' + shlex.join(['git', '-C', str(host),
            'fetch', 'origin']) + ' "$4" || exit 1\nexec ' + shlex.join(
            [sys.executable, str(self.productor), str(self.raiz)]) + ' "$@"\n')
        self.adaptador = self.Jale(self.lanzador, f.git)
        origen, _ = self.adaptador.suite(c, 'lote-r1')
        solicitud = Reproduccion('sub-5', FALLO, 'subconjunto', (5,))
        with patch.object(f.git, 'publicar', wraps=f.git.publicar) as publicar:
            r = self.repeticion_valida(c, origen, solicitud, sub.C, 'lote-r1')
            self.assertEqual(len(publicar.call_args_list), 1)
            nombre, commit = publicar.call_args.args
            self.assertRegex(nombre, r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
            self.assertEqual(commit, sub.C)
            self.assertEqual(f.remoto('refs/heads/tren/' + nombre), sub.C)
            self.assertEqual(self.repeticion_valida(c, origen, solicitud, sub.C, 'lote-r1'), r)
            self.assertTrue(all(llamada.args == (nombre, sub.C)
                                for llamada in publicar.call_args_list))
            self.assertEqual(len(self.lanzamientos()), 4)
            self.repeticion_valida(c, origen, solicitud, sub.C, 'lote-r1', permitir_lanzar=False)
            antes = publicar.call_count
            self.assertIsNone(self.adaptador.repetir(c, origen, replace(solicitud, id='otra'),
                                                    sub.C, 'lote-r1', permitir_lanzar=False))
            self.assertEqual(publicar.call_count, antes)

    def test_r1_publicacion_fallida_no_lanza(self):
        origen = self.origen()
        antes = len(self.lanzamientos())
        git = self.adaptador.git = type('Git', (), {})()
        solicitud = Reproduccion('sub', FALLO, 'subconjunto', (2,))
        for resultado in (ErrorGit('publicacion-rechazada'),
                          RamaOcupada('ref', 'd' * 40, C), None):
            with self.subTest(resultado=resultado), \
                 patch.object(git, 'publicar', create=True, return_value=resultado) as publicar:
                with self.assertRaises(ResultadoInvalido) as contexto:
                    self.adaptador.repetir(CANDIDATO, origen, solicitud, 'd' * 40, 'lote')
                self.assertEqual(contexto.exception.resultado.codigo, 'subconjunto-no-publicado')
                publicar.assert_called_once()
                self.assertEqual(len(self.lanzamientos()), antes)

    def test_r1_subconjunto_sin_git_es_infraestructura(self):
        origen = self.origen()
        self.plan([{}])
        antes = len(self.lanzamientos())
        solicitud = Reproduccion('sub', FALLO, 'subconjunto', (2,))
        self.assertIsNone(self.adaptador.repetir(CANDIDATO, origen, solicitud, 'd' * 40,
                                               'lote', permitir_lanzar=False))
        with self.assertRaises(ResultadoInvalido) as contexto:
            self.adaptador.repetir(CANDIDATO, origen, solicitud, 'd' * 40, 'lote')
        self.assertEqual(contexto.exception.resultado.codigo, 'subconjunto-sin-git')
        self.assertEqual(len(self.lanzamientos()), antes)

    def test_r1_base_transporte_no_envenena_otro_tren(self):
        origen = self.origen()
        nuevo = replace(CANDIDATO, C='e' * 40)
        self.plan([{}])
        ruta, _ = self.adaptador.suite(nuevo, 'otro')
        for codigo in (255, None):
            with self.subTest(codigo=codigo):
                estado = self.raiz / ('estado-' + str(codigo))
                self.adaptador = self.Jale(self.lanzador, estado=estado)
                antes = len(self.lanzamientos())
                self.plan([{}] * antes + [{'codigo': 255, 'sin_linea': True}, {}])
                # U9 carga este archivo sobre una base sin RechazoLanzador y exige que
                # su fallo histórico sea el del segundo tren: el primero se afirma al final.
                try:
                    resultado = self.repetir(origen, 'B')
                    primero = (type(resultado).__name__, resultado.evidencia, resultado.detalle)
                except ResultadoInvalido as error:
                    primero = ('ResultadoInvalido', error.resultado.codigo)
                self.assertEqual(len(self.lanzamientos()), antes + 1)
                registro = Path(self.lanzamientos()[-1][1]).parent
                if codigo is None:
                    (registro / 'codigo.json').write_text('null')
                    with self.assertRaises(ResultadoInvalido) as contexto:
                        self.repetir(origen, 'B')
                    self.assertEqual(contexto.exception.resultado.codigo, 'salida-no-utilizable')
                self.adaptador = self.Jale(self.lanzador, estado=estado)
                r = self.repeticion_valida(nuevo, ruta, Reproduccion('otra', FALLO, 'B'), B, 'otro')
                self.assertEqual(len(self.lanzamientos()), antes + 3)
                self.assertEqual(r.origen_sha, nuevo.C)
                antes = len(self.lanzamientos())
                self.assertEqual(self.repeticion_valida(nuevo, ruta,
                    Reproduccion('tercera', FALLO, 'B'), B, 'tercero', permitir_lanzar=False).unidades,
                    r.unidades)
                self.assertEqual(len(self.lanzamientos()), antes)
                self.assertEqual(primero, ('RechazoLanzador', str(registro / 'coordinador.log'),
                                           'salida-sin-recibo'))

    def test_r1_base_75_no_consume_reanudacion_otro_tren(self):
        origen = self.origen()
        nuevo = replace(CANDIDATO, C='e' * 40)
        ruta, _ = self.adaptador.suite(nuevo, 'otro')
        antes = len(self.lanzamientos())
        self.plan([{}] * (antes + 1) + [{'codigo': 75, 'estados': ['falla']}, {}])
        r = self.repetir(origen, 'B')
        self.assertIsInstance(r, Incompleto)
        self.assertIsNone(self.adaptador.repetir(nuevo, ruta, Reproduccion('otra', FALLO, 'B'),
                                               B, 'otro', permitir_lanzar=False))
        r2 = self.repeticion_valida(nuevo, ruta, Reproduccion('otra', FALLO, 'B'), B, 'otro')
        self.assertEqual(len(self.lanzamientos()), antes + 3)
        self.assertEqual(self.repetir(origen, 'B', intento=1,
                                    reanudar=r.reanudar).unidades, r2.unidades)
        self.assertEqual(len(self.lanzamientos()), antes + 3)

    def test_repeticion_75_reanuda_sin_repetir_primer_intento(self):
        origen = self.origen()
        self.plan([{}, {}, {}, {'codigo': 75, 'sin_linea': True}, {}])
        r = self.repetir(origen)
        self.assertIsInstance(r, Incompleto)
        final = self.repetir(origen, intento=1, reanudar=r.reanudar)
        self.assertEqual([i.veredicto for i in final.unidades[0].intentos], ['pasa', 'pasa'])
        self.assertEqual(len(self.lanzamientos()), 5)

    def test_atribucion_confirma_en_c_y_b_sin_preexistente_prematuro(self):
        origen = self.origen()
        self.plan([{'estados': ['falla']}])
        rc = self.repetir(origen)
        rb = self.repetir(origen, 'B')
        evidencias = tuple(Evidencia(Reproduccion('r' + objetivo, FALLO, objetivo, costo_s=8), r)
                          for objetivo, r in (('C', rc), ('B', rb)))
        p = planificar((FALLO,), (1,), evidencias, 100)
        self.assertTrue(p.terminado)
        self.assertEqual(p.causas, ())

    def test_sin_permiso_no_lanza_y_recupera_recibo_existente(self):
        self.assertIsNone(self.suite(permitir_lanzar=False))
        self.assertEqual(self.lanzamientos(), [])
        original = self.suite()
        self.assertEqual(self.suite(permitir_lanzar=False), original)
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_crash_real_tick_durante_lanzamiento_se_reengancha(self):
        self.plan([{'esperar': True}, {}])
        helper = self.raiz / 'tick.py'
        helper.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) +
            ')\nfrom adaptadores.jale import Jale\nfrom dominio import Candidato\nJale(' +
            repr(str(self.lanzador)) + ').suite(Candidato(' + repr(B) + ', (), ' + repr(C) +
            ', "d" * 40), "lote")\n')
        proceso = subprocess.Popen([sys.executable, str(helper)])
        try:
            limite = time.monotonic() + 10
            while not (self.raiz / 'listo').exists() and proceso.poll() is None and time.monotonic() < limite:
                time.sleep(.02)
            self.assertTrue((self.raiz / 'listo').exists())
        finally:
            proceso.kill()
            proceso.wait(timeout=5)
            (self.raiz / 'seguir').write_text('seguir')
        _, r = self.suite()
        self.assertEqual(r.veredicto, 'verde')
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_coordinador_interrumpido_no_duplica_lanzamiento_incierto(self):
        registro = self.adaptador.estado / clave('suite', 'lote') / 'contract' / '0'
        registro.mkdir(parents=True)
        (registro / 'solicitud.json').write_text(json.dumps(['bash', str(self.lanzador.resolve()),
            'auto', str(registro / 'corrida.log'), 'contract', C]))
        (registro / 'iniciado.json').write_text('true')
        with self.assertRaises(ResultadoInvalido) as contexto:
            self.suite()
        self.assertEqual(contexto.exception.resultado.codigo, 'corrida-interrumpida')
        self.assertEqual(self.lanzamientos(), [])

    def test_u23_muerte_del_grupo_del_coordinador_recupera_el_mismo_trabajo(self):
        self.plan([{'esperar': True}, {}])
        helper = self.raiz / 'tick-u23.py'
        helper.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) +
            ')\nfrom adaptadores.jale import Jale\nfrom dominio import Candidato\nJale(' +
            repr(str(self.lanzador)) + ', modos=("contract",)).suite(Candidato(' + repr(B) + ', (), ' + repr(C) +
            ', "d" * 40), "lote")\n')
        with (self.raiz / 'tick-u23.log').open('w') as salida:
            proceso = subprocess.Popen([sys.executable, str(helper)], stdout=salida, stderr=salida)
            try:
                limite = time.monotonic() + 10
                while not (self.raiz / 'listo').exists() and proceso.poll() is None and time.monotonic() < limite:
                    time.sleep(.02)
                self.assertTrue((self.raiz / 'listo').exists())
            finally:
                if proceso.poll() is None:
                    proceso.kill()
                proceso.wait(timeout=5)
                invocaciones = self.invocaciones()
                if invocaciones:
                    grupo = invocaciones[0]['pgid']
                    self.assertNotEqual(grupo, os.getpgrp())
                    try:
                        os.killpg(grupo, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                (self.raiz / 'seguir').write_text('seguir')
        registro = Path(self.lanzamientos()[0][1]).parent
        self.assertFalse((registro / 'codigo.json').exists())
        iniciado = (registro / 'iniciado.json').read_text()
        # Recuperar trabajo autorizado tampoco requiere admisión para lanzar uno nuevo.
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        _, r = self.suite(permitir_lanzar=False)
        self.assertEqual(r.veredicto, 'verde')
        self.assertEqual((registro / 'iniciado.json').read_text(), iniciado)
        self.assertEqual(json.loads(iniciado), {'clave': self.invocaciones()[0]['clave']})
        self.assertEqual(self.invocaciones()[0]['clave'], self.invocaciones()[1]['clave'])
        self.assertEqual(self.invocaciones()[0]['log'], self.invocaciones()[1]['log'])
        self.assertIn('PUEUE_REENGANCHE=0', (registro / 'coordinador.log').read_text())
        self.assertEqual(len(self.invocaciones()), 2)
        self.assertEqual(len(self.lanzamientos()), 1)
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 0)
        self.assertEqual(self.suite()[1], r)
        self.assertEqual(len(self.invocaciones()), 2)

    def test_u23_clave_estable_y_distinta_por_intento_modo_y_tren(self):
        self.plan([{'codigo': 75, 'sin_linea': True}, {}, {}, {}, {}])
        _, incompleto = self.suite()
        primero = self.invocaciones()[0]
        registro = Path(primero['log']).parent
        iniciado = json.loads((registro / 'iniciado.json').read_text())
        self.assertEqual(iniciado, {'clave': primero['clave']})
        self.assertIsInstance(incompleto, Incompleto)
        self.assertEqual(self.suite()[1], incompleto)
        self.assertEqual(json.loads((registro / 'iniciado.json').read_text()), iniciado)
        with patch.dict(os.environ, {'REMOTE_RUN_CLAVE': 'clave-del-padre'}):
            ruta, final = self.suite(intento=1, reanudar=incompleto.reanudar)
            self.assertEqual(final.veredicto, 'verde')
            self.assertEqual(self.suite(intento=1), (ruta, final))
            self.assertEqual(self.suite(tren='otro')[1].veredicto, 'verde')
        claves = [i['clave'] for i in self.invocaciones()]
        self.assertEqual(len(claves), 5)
        self.assertEqual(len(set(claves)), 5)
        for valor in claves:
            self.assertRegex(valor, r'^[A-Za-z0-9._-]{8,128}$')
            self.assertNotEqual(valor, 'clave-del-padre')

    def test_u23_transporte_255_reengancha_con_la_misma_clave(self):
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        self.plan([{'transportes': 2}])
        ruta, r = self.suite()
        self.assertEqual(r.veredicto, 'verde')
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 3)
        self.assertEqual(len({i['clave'] for i in invocaciones}), 1)
        self.assertRegex(invocaciones[0]['clave'], r'^[A-Za-z0-9._-]{8,128}$')
        self.assertEqual(len(self.lanzamientos()), 1)
        registro = Path(invocaciones[0]['log']).parent
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 0)
        self.assertEqual(self.suite(), (ruta, r))
        self.assertEqual(len(self.invocaciones()), 3)

    def test_sen7124_transporte_127_reengancha_con_la_misma_clave(self):
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        self.plan([{'transportes': 1, 'codigo_transporte': 127}])
        _, resultado = self.suite()
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        primera, segunda = self.invocaciones()
        self.assertRegex(primera['clave'], r'^[A-Za-z0-9._-]{8,128}$')
        self.assertEqual((primera['clave'], primera['log']), (segunda['clave'], segunda['log']))
        self.assertEqual(len(self.lanzamientos()), 1)
        log = Path(primera['log'])
        self.assertEqual(log.with_name('coordinador.log').read_text().splitlines(),
                         ['PUEUE_ID=0', 'Wsl/Service/WSAETIMEDOUT', 'PUEUE_REENGANCHE=0'])
        self.assertEqual(json.loads(log.with_name('codigo.json').read_text()), 0)

    def test_sen7279_repite_el_tick_tras_archivar_sin_reabrir_otra_salida(self):
        from dominio import RechazoLanzador
        transporte = {'transportes': 3, 'codigo_transporte': 127}
        self.plan([transporte, transporte])
        _, rechazo = self.suite()
        self.assertIsInstance(rechazo, RechazoLanzador)
        self.assertEqual(rechazo.detalle, 'salida-sin-recibo')
        registro = Path(rechazo.evidencia).parent
        codigo = registro / 'codigo.json'
        anterior = registro / 'codigo-reenganche-1.json'
        reenganche = (1, rechazo.evidencia)
        self.assertIsNone(self.suite(reenganche=reenganche, permitir_lanzar=False))
        self.assertEqual(json.loads(codigo.read_text()), 127)
        with patch('adaptadores.jale.subprocess.Popen', side_effect=RuntimeError('tick interrumpido')):
            with self.assertRaisesRegex(RuntimeError, 'tick interrumpido'):
                self.suite(reenganche=reenganche)
        self.assertFalse(codigo.exists())
        self.assertEqual(json.loads(anterior.read_text()), 127)
        self.assertEqual(len(self.invocaciones()), 3)

        _, segundo_rechazo = self.suite(reenganche=reenganche)
        self.assertEqual(segundo_rechazo.detalle, 'salida-sin-recibo')
        self.assertNotEqual(segundo_rechazo.evidencia, rechazo.evidencia)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 7)
        self.assertEqual({(i['log'], i['clave']) for i in invocaciones[:4]},
                         {(str(registro / 'corrida.log'), invocaciones[0]['clave'])})
        self.assertEqual(json.loads(codigo.read_text()), 0)
        self.assertEqual(self.suite(reenganche=reenganche)[1], segundo_rechazo)
        self.assertEqual(self.invocaciones(), invocaciones)

        _, resultado = self.suite(reenganche=(2, segundo_rechazo.evidencia))
        self.assertEqual(resultado, Completo('verde', costo_s=8))
        self.assertEqual(len(self.lanzamientos()), 2)
        self.assertEqual(self.invocaciones()[-1]['clave'], invocaciones[-1]['clave'])
        self.assertEqual(json.loads(anterior.read_text()), 127)

    def test_sen7279_reengancha_con_el_lanzador_vigente_del_operador(self):
        from unittest.mock import Mock
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        viejo = self.raiz / 'snapshot-viejo.sh'
        vigente = self.raiz / 'snapshot-vigente.sh'
        viejo.write_text(self.lanzador.read_text())
        vigente.write_text(self.lanzador.read_text())
        operador = Mock(checkout=self.raiz)
        operador.lanzador.return_value = str(viejo)
        operador.entorno_lanzador.return_value = None
        self.adaptador.operador = operador
        self.plan([{'transportes': 3, 'codigo_transporte': 127}])
        _, rechazo = self.suite()
        self.assertEqual(rechazo.detalle, 'salida-sin-recibo')
        registro = Path(rechazo.evidencia).parent
        solicitud = registro / 'solicitud.json'
        argv = json.loads(solicitud.read_text())
        self.assertEqual(argv[1], str(viejo))
        iniciado = (registro / 'iniciado.json').read_bytes()
        viejo.write_text('#!/bin/bash\nexit 64\n')
        operador.lanzador.return_value = str(vigente)
        self.adaptador.host = 'desktop'
        reenganche = (1, rechazo.evidencia)

        with patch('adaptadores.jale.subprocess.Popen', side_effect=RuntimeError('tick interrumpido')):
            with self.assertRaisesRegex(RuntimeError, 'tick interrumpido'):
                self.suite(reenganche=reenganche)
        self.assertEqual(json.loads(solicitud.read_text()), [argv[0], str(vigente), *argv[2:]])
        operador.lanzador.assert_called_with(str(self.lanzador), 'lote', registro / 'codigo.json')
        _, resultado = self.suite(reenganche=reenganche)
        operador.validar_guardado.assert_called_with(str(vigente))
        self.assertEqual(operador.lanzador.call_count, 2)
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        self.assertEqual((registro / 'iniciado.json').read_bytes(), iniciado)
        self.assertEqual(json.loads((registro / 'codigo-reenganche-1.json').read_text()), 127)
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 0)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 4)
        self.assertEqual({Path(i['log']).parent.name for i in invocaciones}, {'0'})
        self.assertEqual({i['clave'] for i in invocaciones}, {json.loads(iniciado)['clave']})
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_sen7279_reengancha_repeticion_de_base_y_comparte_recibo(self):
        from dominio import RechazoLanzador
        origen = self.origen()
        self.plan([{}, {}, {}, {'transportes': 3, 'codigo_transporte': 127}])
        rechazo = self.repetir(origen, 'B')
        self.assertIsInstance(rechazo, RechazoLanzador)
        self.assertEqual(rechazo.detalle, 'salida-sin-recibo')
        registro = Path(rechazo.evidencia).parent
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 127)

        resultado = self.repetir(origen, 'B', reenganche=(1, rechazo.evidencia))
        self.assertIsInstance(resultado, Repeticion)
        self.assertEqual(json.loads((registro / 'codigo-reenganche-1.json').read_text()), 127)
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 0)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 7)
        self.assertEqual({(i['log'], i['clave']) for i in invocaciones[-4:]},
                         {(str(registro / 'corrida.log'), invocaciones[-4]['clave'])})
        self.assertEqual(len(self.lanzamientos()), 4)
        otro = self.adaptador.repetir(CANDIDATO, origen, Reproduccion('otra', FALLO, 'B'),
                                      B, 'otro', permitir_lanzar=False)
        self.assertEqual(otro.unidades, resultado.unidades)
        self.assertEqual(self.invocaciones(), invocaciones)

    def test_sen7279_reengancha_repeticion_aunque_otro_tren_complete_el_cache(self):
        origen = self.origen()
        self.plan([{}, {}, {}, {'transportes': 3, 'codigo_transporte': 127}, {}])
        rechazo = self.repetir(origen, 'B')
        clave_original = self.invocaciones()[-1]['clave']
        solicitud = Reproduccion('otra', FALLO, 'B')
        otro = self.repeticion_valida(CANDIDATO, origen, solicitud, B, 'otro')
        invocaciones = self.invocaciones()

        resultado = self.repetir(origen, 'B', reenganche=(1, rechazo.evidencia))
        registro = Path(rechazo.evidencia).parent
        self.assertIsInstance(resultado, Repeticion)
        self.assertEqual(json.loads((registro / 'codigo-reenganche-1.json').read_text()), 127)
        reenganchada, = self.invocaciones()[len(invocaciones):]
        self.assertEqual((reenganchada['log'], reenganchada['clave']),
                         (str(registro / 'corrida.log'), clave_original))
        self.assertEqual(len(self.lanzamientos()), 5)
        self.assertEqual(self.repeticion_valida(CANDIDATO, origen, solicitud,
            B, 'tercero', permitir_lanzar=False).unidades, otro.unidades)

    def test_u23_transporte_255_agota_limite_y_conserva_salida(self):
        from dominio import RechazoLanzador
        self.plan([{'codigo': 255, 'sin_linea': True}])
        recibo = self.suite()
        self.assertEqual(self.suite(), recibo)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 3)
        self.assertEqual(len({i['clave'] for i in invocaciones}), 1)
        self.assertEqual(len(self.lanzamientos()), 1)
        registro = Path(invocaciones[0]['log']).parent
        self.assertEqual(recibo[1], RechazoLanzador(str(registro / 'coordinador.log'),
                                                  'salida-sin-recibo'))
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 255)

    def test_u23_125_se_guarda_sin_reintento_con_la_misma_clave(self):
        from dominio import RechazoLanzador
        self.plan([{'codigo': 125, 'sin_linea': True}, {}, {}])
        recibo = self.suite()
        self.assertEqual(self.suite(), recibo)
        self.assertEqual(len(self.invocaciones()), 1)
        registro = Path(self.invocaciones()[0]['log']).parent
        self.assertEqual(recibo[1], RechazoLanzador(str(registro / 'coordinador.log'),
                                                  'salida-sin-recibo'))
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 125)
        self.assertEqual(self.suite(intento=1)[1].veredicto, 'verde')
        self.assertEqual(len(self.invocaciones()), 3)
        self.assertEqual(len({i['clave'] for i in self.invocaciones()}), 3)

    def test_seleccion_configurable_y_hasito_legacy(self):
        from adaptadores import seleccionar
        git = object()
        legacy = seleccionar({'lanzador_hasito': str(self.lanzador)}, git)
        self.assertIsInstance(legacy, Hasito)
        self.assertIs(legacy.git, git)
        self.assertEqual(legacy.lanzador, str(self.lanzador.resolve()))
        jale = seleccionar({'adaptador': 'jale', 'lanzador': str(self.lanzador),
                            'host': 'auto', 'modos': ['e2e']}, git)
        self.assertIsInstance(jale, self.Jale)
        self.assertEqual(jale.modos, ('e2e',))
        self.assertEqual(jale.suite(CANDIDATO, 'config')[1].costo_s, 4)
        self.assertIsNone(seleccionar({}, git))
        for config in ({'adaptador': 'otro'}, {'adaptador': 'jale'},
                       {'adaptador': 'jale', 'lanzador': 'relativo'},
                       {'adaptador': 'jale', 'lanzador': str(self.lanzador), 'host': 'invalido'},
                       {'adaptador': 'jale', 'lanzador': str(self.lanzador), 'modos': []}):
            with self.assertRaises(ValueError):
                seleccionar(config, git)

    def test_cli_carga_jale_desde_config_privada(self):
        args = cli.parser().parse_args(['tick', '--repo', REPO])
        config = {'remoto': 'origen', 'adaptador': 'jale', 'lanzador': str(self.lanzador)}
        with patch.object(cli, 'activacion', return_value='activo'), \
             patch.object(cli, 'configuracion', return_value=config), \
             patch.object(cli, 'GitHubGit'), patch('git_ops.GitTren'), \
             patch('operativo.tick', return_value={}) as tick:
            self.assertEqual(cli.ejecutar(args), (0, {}))
        self.assertIsInstance(tick.call_args.args[5], self.Jale)

    def test_efectos_consumen_jale_y_tipan_infraestructura(self):
        inicial = estado()
        tren = Tren('lote', (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO))
        inicial = replace(inicial, trenes=(tren,))
        efecto = Efecto(clave('suite', 'lote'), 'iniciar-suite', ('lote',))
        obs = observacion(ci_candidato='verde')
        self.plan([{'cambiar': {'sha': B}}])
        with self.assertRaises(EfectoFallido) as contexto:
            ejecutar(efecto, inicial, obs, object(), adaptador=self.adaptador)
        self.assertEqual(contexto.exception.resultado.codigo, 'sha-no-corresponde')
