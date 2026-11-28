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
from unittest.mock import Mock, patch

from test_tren import Aislado, cli, estado, observacion, REPO
from test_hasito import B, C, CANDIDATO
from adaptadores.hasito import Hasito, ResultadoInvalido
from dominio import (Admitido, Certificado, Completo, Decision, Efecto, EsperandoCI, Evidencia,
                     Fallo, Incompleto, Intento, Pausa, RechazoLanzador, Repeticion,
                     Reproduccion, Terminal, Tren, clave, datos, desde_datos, intento_suite, paso)
from efectos import EfectoFallido, ejecutar
from atribucion import planificar
from git_ops import ErrorGit, Publicado, RamaOcupada

UNIDAD = 'apps/api/src/a.contract.test.ts'
OTRA = 'apps/api/src/b.contract.test.ts'
FIRMA = 'sha256:' + 'a' * 64
FALLO = Fallo(UNIDAD, FIRMA, 'solo-candidato', 'contract', 'contract', 'jale', 4)
CASO = 'src/a.contract.test.ts › acceso › entra & sale'
SIN_CASOS = object()

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
    boat = {k: os.environ.get(k) for k in ('REMOTE_RUN_CLAVE', 'SEN_E2E_FRAGMENTO',
        'SEN_RUN_OWNER', 'SEN_RUN_ID', 'SEN_BOAT_PRIORIDAD', 'SEN_BOAT_ESPERAR', 'TURBO_FORCE')}
    clave = boat['SEN_RUN_ID'] if host == 'boat' else boat['REMOTE_RUN_CLAVE'] or ''
    with invocaciones.open('a') as archivo:
        archivo.write(json.dumps(dict(clave=clave, log=log, pgid=os.getpgrp(), modo=modo,
                                      host=host, boat=boat)) + '\n')
    registros = raiz / 'por-clave'
    registros.mkdir(exist_ok=True)
    identidad = clave or 'sin-clave-' + str(len(previas))
    if host == 'boat':
        assert boat['REMOTE_RUN_CLAVE'] is None and boat['SEN_RUN_OWNER'] == 'pao-tren'
        identidad = json.dumps([clave, modo, sha, filtros])
    registro = registros / (hashlib.sha256(identidad.encode()).hexdigest() + '.json')
    conocido = registro.exists()
    if conocido:
        trabajo = json.loads(registro.read_text())
        print('PUEUE_REENGANCHE=' + str(trabajo['id']), flush=True)
    else:
        previos = contador.read_text().splitlines() if contador.exists() else []
        plan = json.loads((raiz / 'plan.json').read_text())
        indice = len(previos)
        if isinstance(plan, dict):
            indice = sum(json.loads(fila)[2] == modo for fila in previos)
            plan = plan.get(modo, [{}])
        trabajo = dict(id=len(previos), conexiones=0, plan=plan[min(indice, len(plan) - 1)])
        with contador.open('a') as archivo:
            archivo.write(json.dumps([host, log, modo, sha, *filtros]) + '\n')
    trabajo['conexiones'] += 1
    registro.write_text(json.dumps(trabajo))
f = trabajo['plan']
log = Path(log)
sidecar = log.with_name(log.name + '.boat-pendiente')
run_id = 'jale-' + registro.stem[:40]
if trabajo['conexiones'] <= f.get('transportes', 0):
    codigo = f.get('codigo_transporte', 255)
    if codigo == 127:
        if not conocido:
            print('PUEUE_ID=' + str(trabajo['id']), flush=True)
        print('Wsl/Service/WSAETIMEDOUT', file=sys.stderr, flush=True)
    if host == 'boat' and f.get('fallback'):
        log.write_text(f'HOST=desktop fallback=sin-lugar run_id={run_id}\n')
    elif host != 'boat':
        log.write_text('')
    sys.exit(codigo)
if 'pendiente' in f and (f.get('pendiente_fijo') or
        trabajo['conexiones'] <= f.get('transportes', 0) + f.get('pendientes', 1)):
    sidecar.write_text(json.dumps(dict(run_id=run_id, cause=f['pendiente'], status='pending')))
    sys.exit(75)
if f.get('fallback_75') and trabajo['conexiones'] == 1:
    linea = f'HOST=desktop fallback=sin-lugar run_id={run_id}\n'.encode()
    log.write_bytes(linea + (b'salida: \xff\n' if f.get('log_no_utf8') else b''))
    sys.exit(75)
if f.get('barrera'):
    (raiz / ('listo-' + modo)).touch()
    otro = raiz / ('listo-e2e' if modo == 'contract' else 'listo-contract')
    limite = time.monotonic() + 10
    while not otro.exists() and time.monotonic() < limite:
        time.sleep(.02)
    if not otro.exists():
        sys.exit(2)
    (raiz / ('cruzado-' + modo)).touch()
if f.get('esperar'):
    sufijo = '-' + log.parent.parent.name if filtros and f.get('esperar_intento') else ''
    ((log.parent if sufijo else raiz) / 'listo').touch()
    limite = time.monotonic() + f.get('plazo', 20)
    while not (raiz / ('seguir' + sufijo)).exists() and time.monotonic() < limite:
        time.sleep(.02)
rutas = filtros or (["apps/api/src/a.contract.test.ts"] if modo == 'contract'
                     else ['apps/web/e2e/a.spec.ts'])
estados = f.get('estados', ['pasa'] * len(rutas))
if len(estados) > len(rutas):
    rutas.append('apps/api/src/b.contract.test.ts')
unidades = [dict(unidad=u, resultado=e, firma='sha256:' + 'a' * 64 if e == 'falla' else '',
                 fallidas=int(e == 'falla'), duracion_s=0 if e == 'no-corrida' else 4)
            for u, e in zip(rutas, estados)]
if 'casos' in f:
    unidades[0]['casos'] = f['casos']
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
contenido = json.dumps(r)
if f.get('campo_duplicado'):
    campo = json.dumps(f['campo_duplicado']) + ': '
    contenido = contenido.replace(campo, campo + 'null, ' + campo, 1)
linea = 'RECIBO jale-recibo@1 ' + base64.b64encode(contenido.encode()).decode() + '\n'
if f.get('malformado'):
    linea = 'RECIBO jale-recibo@1 xxx\n'
if f.get('duplicar_recibo'):
    linea *= 2
if f.get('sin_linea'):
    linea = ''
if f.get('interrumpida'):
    linea += 'PUEUE_INTERRUPTED=1\n'
if host == 'boat':
    encabezado = 'HOST=desktop fallback=sin-lugar' if f.get('fallback') else 'HOST=boat fallback=none'
    linea = f'{encabezado} run_id={run_id}\n' + f.get('cobertura', '') + linea
    sidecar.unlink(missing_ok=True)
if f.get('terminal'):
    codigo_suite = f.get('codigo_suite', codigo)
    linea += f'RESULT mode={modo} exit={codigo_suite}\nEXIT={codigo_suite}\n'
    if codigo_suite != codigo:
        linea += f'PUEUE_EXIT={codigo}\n'
salida = linea.encode()
if f.get('aviso'):
    salida = b'Aviso del lanzador\n' + salida
if f.get('log_no_utf8'):
    salida += b'salida: \xff\n'
if f.get('recibo_no_utf8'):
    salida = salida.replace(b'RECIBO jale-recibo@1 ', b'RECIBO jale-recibo@1 \xff', 1)
log.write_bytes(salida)
sys.exit(codigo)
'''


def esperar_resultado(llamar, *args, **kwargs):
    from adaptadores.jale import EN_CURSO

    limite = time.monotonic() + 25
    while True:
        resultado = llamar(*args, **kwargs)
        if resultado is not EN_CURSO and not getattr(resultado, 'en_curso', False):
            return resultado
        if time.monotonic() >= limite:
            raise AssertionError('el coordinador no dejó resultado')
        time.sleep(.025)


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

    ejecutar_hasta_resultado = staticmethod(esperar_resultado)

    def plan(self, filas):
        (self.raiz / 'plan.json').write_text(json.dumps(filas))

    def lanzamientos(self):
        ruta = self.raiz / 'lanzamientos.jsonl'
        return [json.loads(l) for l in ruta.read_text().splitlines()] if ruta.exists() else []

    def invocaciones(self):
        ruta = self.raiz / 'invocaciones.jsonl'
        return [json.loads(l) for l in ruta.read_text().splitlines()] if ruta.exists() else []

    def suite(self, tren='lote', **kwargs):
        return esperar_resultado(self.adaptador.suite, CANDIDATO, tren, **kwargs)

    def origen(self):
        self.plan({'contract': [{'estados': ['falla']}]})
        return self.suite()[0]

    def repetir(self, origen, objetivo='C', **kwargs):
        return esperar_resultado(self.adaptador.repetir, CANDIDATO, origen,
            Reproduccion('solicitud-' + objetivo, FALLO, objetivo),
            B if objetivo == 'B' else C, 'lote', **kwargs)

    def invalido(self, fila, codigo=None):
        self.plan({'contract': [fila]})
        with self.assertRaises(ResultadoInvalido) as contexto:
            self.suite()
        if codigo:
            self.assertEqual(contexto.exception.resultado.codigo, codigo)
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def configurar_rechazo(self, respuesta, *, interrumpida=False):
        filas = {
            '64': {'codigo': 64, 'sin_linea': True},
            '75': {'codigo': 75, 'sin_linea': True},
            '127': {'transportes': 3, 'codigo_transporte': 127},
            '125': {'codigo': 125, 'sin_linea': True, 'interrumpida': interrumpida},
            'rojo': {'estados': ['falla']},
        }
        contract = sum(a[2] == 'contract' for a in self.lanzamientos())
        self.plan({'contract': [{}] * contract + [filas.get(respuesta, {})]})

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

    def test_repeticion_sin_recibo_absuelta_desde_diagnostico(self):
        self.verificar_rechazo('repetir', codigo=127, decision='absolver')

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
        self.assertEqual(sorted((a[0], a[2:]) for a in self.lanzamientos()),
                         [('auto', ['contract', C]), ('auto', ['e2e', C])])
        self.assertEqual(self.suite(), (ruta, r))
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_suite_roja_firma_y_no_corrida_sin_preexistentes(self):
        self.plan({'contract': [{'estados': ['falla', 'no-corrida']}]})
        _, r = self.suite()
        self.assertEqual((r.veredicto, r.fallos, r.restantes), ('rojo', (FALLO,), (OTRA,)))

    def test_75_sin_recibo_reanuda_sin_repetir_modo_terminado(self):
        self.plan({'e2e': [{'codigo': 75, 'sin_linea': True}, {}]})
        _, r = self.suite()
        self.assertIsInstance(r, Incompleto)
        _, final = self.suite(intento=1, reanudar=r.reanudar)
        self.assertEqual(final.veredicto, 'verde')
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e', 'e2e'])

    def test_75_con_recibo_no_se_usa_como_fallo(self):
        self.plan({'contract': [{'codigo': 75, 'estados': ['falla']}]})
        self.assertIsInstance(self.suite()[1], Incompleto)

    def test_paro_64_no_reintenta_ni_cambia_host(self):
        self.plan({'contract': [{'codigo': 64, 'sin_linea': True}]})
        recibo = self.suite()
        contract, = [a for a in self.lanzamientos() if a[2] == 'contract']
        evidencia = str(Path(contract[1]).with_name('coordinador.log'))
        self.assertEqual(recibo[1], RechazoLanzador(evidencia, 'lanzador-rechazo'))
        self.assertEqual(desde_datos({'_tipo': 'RechazoLanzador', 'evidencia': evidencia}),
                         recibo[1])
        self.assertEqual(self.suite(), recibo)
        self.assertCountEqual([(a[0], a[2]) for a in self.lanzamientos()],
                              [('auto', 'contract'), ('auto', 'e2e')])

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
        self.plan({'e2e': [{'estados': ['falla'], 'codigo': 0}]})
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
        self.plan({'contract': [{'sin_linea': True}]})
        _, resultado = self.suite()
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])
        contract, = [a for a in self.lanzamientos() if a[2] == 'contract']
        evidencia = str(Path(contract[1]).with_name('coordinador.log'))
        self.assertEqual(resultado, RechazoLanzador(evidencia, 'salida-sin-recibo'))

    def test_base64_malformado(self):
        self.plan({'contract': [{'malformado': True}]})
        _, resultado = self.suite()
        contract = next(i for i in self.invocaciones() if i['modo'] == 'contract')
        self.assertEqual(resultado, RechazoLanzador(contract['log'], 'recibo-malformado'))
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def test_leer_recibo_malformado_conserva_el_log(self):
        from adaptadores.jale import leer_recibo

        log = self.raiz / 'corrida.log'
        prefijo = b'RECIBO jale-recibo@1 '
        casos = {
            'formato': b'RECIBO jale-recibo@1\n',
            'base64': prefijo + b'xxx\n',
            'cortado con texto pegado': prefijo + b'eyJlc3FRESULT mode=contract exit=0\n',
            'json': prefijo + b'ew==\n',  # {
            'payload no UTF-8': prefijo + b'/w==\n',
            'log no UTF-8': prefijo + b'\xff\n',
        }
        for caso, salida in casos.items():
            with self.subTest(caso=caso):
                log.write_bytes(salida)
                self.assertEqual(leer_recibo(log, 0, C, 'contract', (), host='desktop'),
                                 RechazoLanzador(str(log), 'recibo-malformado'))

    def resolver_recibo_malformado(self, decision):
        from adaptadores.jale import EN_CURSO
        from observador import Falso
        from operativo import tick
        from test_hasito import GitFalso
        from test_tren import vista

        self.plan({'contract': [{'malformado': True}, {}]})
        ledger = self.ledger()
        inicial = estado(len(CANDIDATO.eslabones))
        entradas = tuple(replace(e, entrega=replace(e.entrega, head=head),
                                 admision=Admitido(), tren='lote')
                         for e, (_, head, _) in zip(inicial.entradas, CANDIDATO.eslabones))
        recibos = tuple(e.entrega.recibo for e in entradas)
        tren = Tren('lote', recibos, EsperandoCI(CANDIDATO))
        inicial = replace(inicial, entradas=entradas, trenes=(tren,))
        for entrada in entradas:
            ledger.entregar(entrada.entrega, entrada.recibida)
        obs = observacion(ci_candidato='verde', ci_recibo='recibo-ci')
        obs = replace(obs, prs=tuple(vista(e.entrega.pr, head=e.entrega.head) for e in entradas))
        observador = Falso(obs)
        actual, (efecto,) = paso(inicial, obs)
        ledger.guardar_paso(actual, (efecto,), obs.ahora)

        ejecutados = []
        def avanzar(ahora):
            def frontera(nombre, efecto):
                if nombre == 'antes-ejecutar':
                    ejecutados.append(efecto)
            tick(ledger, REPO, observador, ahora, GitFalso(self.raiz), self.adaptador,
                 frontera=frontera)
            return ledger.estado(REPO, operativo=True)

        def esperar_corrida(modo, intento):
            log = self.adaptador.estado / clave('suite', tren.id) / modo / str(intento) / 'corrida.log'
            esperar_resultado(lambda: log if log.with_name('codigo.json').exists() else EN_CURSO)
            return log

        avanzar(101)
        log = esperar_corrida('contract', 0)
        esperar_corrida('e2e', 0)
        pausado = avanzar(102)
        self.assertEqual(pausado.pausa, Pausa('infraestructura', detalle='recibo-malformado',
                                             archivos=(str(log),)))
        self.assertFalse(ledger.outbox(REPO))
        ejecutados.clear()
        invocaciones = self.invocaciones()
        self.assertEqual(avanzar(103).pausa, pausado.pausa)
        self.assertEqual(ejecutados, [])
        self.assertEqual(self.invocaciones(), invocaciones)

        args = cli.parser().parse_args(['juicio', '--repo', REPO, '--tren', tren.id,
                                        '--decision', decision, '--motivo', 'prueba'])
        with patch.object(cli, 'activacion', return_value='activo'):
            codigo, resultado = cli.ejecutar(args, ahora=104)
        self.assertEqual(codigo, 0, resultado)
        resuelto = avanzar(105)
        self.assertIsNone(resuelto.pausa)
        if decision == 'abortar':
            self.assertEqual(resuelto.trenes[0].fase, Terminal('ABORTADO'))
            self.assertEqual(tuple(e.entrega.recibo for e in resuelto.entradas), recibos)
            self.assertTrue(all(e.tren is None and e.terminal is None for e in resuelto.entradas))
            self.assertEqual(self.invocaciones(), invocaciones)
        else:
            nuevo_log = esperar_corrida('contract', 1)
            final = avanzar(106)
            self.assertIsInstance(final.trenes[0].fase, Certificado)
            self.assertEqual([i['log'] for i in self.invocaciones()[len(invocaciones):]],
                             [str(nuevo_log)])

    def test_recibo_malformado_pausa_y_reanudar_produce_veredicto(self):
        self.resolver_recibo_malformado('reanudar')

    def test_recibo_malformado_pausa_y_abortar_devuelve_la_fila(self):
        self.resolver_recibo_malformado('abortar')

    def test_recibo_duplicado(self):
        self.invalido({'duplicar_recibo': True}, 'salida-sin-recibo')

    def test_campo_duplicado_fuera_de_casos_invalida_recibo(self):
        self.plan({'contract': [{'campo_duplicado': 'codigo'}]})
        _, resultado = self.suite()
        contract = next(i for i in self.invocaciones() if i['modo'] == 'contract')
        self.assertEqual(resultado, RechazoLanzador(contract['log'], 'recibo-malformado'))

    def repeticion_paralela_malformada(self, malformado):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',),
                                   repeticiones_paralelas=2)
        origen = self.origen()
        solicitud = Reproduccion('solicitud-C', FALLO, 'C')
        raiz, _, _ = self.adaptador.preparar_repeticion(CANDIDATO, origen, solicitud, C, 'lote')
        logs = {}
        for n in (1, 2):
            registro = raiz / str(n) / '0'
            registro.mkdir(parents=True)
            log = logs[n] = registro / 'corrida.log'
            (registro / 'solicitud.json').write_text(json.dumps([
                'bash', str(self.lanzador), 'boat', str(log), 'contract', C, FALLO.unidad]))
            (registro / 'codigo.json').write_text('1' if n == malformado else '75')
            if n == malformado:
                log.write_text('RECIBO jale-recibo@1 xxx\nRESULT mode=contract exit=1\nEXIT=1\n')
            else:
                (registro / 'corrida.log.boat-pendiente').write_text(json.dumps(
                    {'status': 'pending', 'cause': 'resultado o transporte no acreditado'}))
        resultado = self.repetir(origen)
        self.assertIsInstance(resultado, RechazoLanzador)
        self.assertEqual(resultado.detalle, 'recibo-malformado')

    def test_repeticion_paralela_malformado_en_el_primer_intento_decide(self):
        self.repeticion_paralela_malformada(1)

    def test_repeticion_paralela_malformado_tras_un_75_decide(self):
        self.repeticion_paralela_malformada(2)

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
        ruta, _ = esperar_resultado(self.adaptador.suite, nuevo, 'otro')
        antes = len(self.lanzamientos())
        r2 = esperar_resultado(self.Jale(self.lanzador).repetir, nuevo, ruta,
            Reproduccion('otra', FALLO, 'B'), B, 'otro', permitir_lanzar=False)
        self.assertEqual(len(self.lanzamientos()), antes)
        self.assertEqual(r2.unidades, r.unidades)
        self.assertEqual(r2.origen_sha, nuevo.C)
        self.assertNotEqual(r2.origen_sha256, r.origen_sha256)

    def test_repeticion_con_registro_de_base_en_curso(self):
        from adaptadores.jale import EN_CURSO
        origen = self.origen()
        self.plan([{'esperar': True}])
        estado = self.adaptador.estado
        registro = estado / clave('repetir', 'otro', 'solicitud-B') / '1' / '0'
        proceso = self.adaptador.lanzar(registro, B, 'contract', (UNIDAD,), True)
        try:
            cache = estado / clave('base', B, FALLO.check, FALLO.unidad) / '1' / 'registro.json'
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(str(registro.relative_to(estado))))
            solicitud = Reproduccion('solicitud-B', FALLO, 'B')
            resultado = self.adaptador.repetir(CANDIDATO, origen, solicitud, B, 'lote',
                                              permitir_lanzar=False)
            self.assertIs(resultado, EN_CURSO)
        finally:
            (self.raiz / 'seguir').touch()
            proceso.wait(timeout=5)

    def repeticion_valida(self, candidato, origen, solicitud, commit, tren, **kwargs):
        try:
            r = esperar_resultado(self.adaptador.repetir, candidato, origen, solicitud, commit, tren, **kwargs)
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
            'fetch', '--no-write-fetch-head', 'origin']) + ' "$4" || exit 1\nexec ' + shlex.join(
            [sys.executable, str(self.productor), str(self.raiz)]) + ' "$@"\n')
        self.adaptador = self.Jale(self.lanzador, f.git)
        origen, _ = esperar_resultado(self.adaptador.suite, c, 'lote-r1')
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
            self.assertIsNone(esperar_resultado(self.adaptador.repetir, c, origen, replace(solicitud, id='otra'),
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
                    esperar_resultado(self.adaptador.repetir, CANDIDATO, origen, solicitud, 'd' * 40, 'lote')
                self.assertEqual(contexto.exception.resultado.codigo, 'subconjunto-no-publicado')
                publicar.assert_called_once()
                self.assertEqual(len(self.lanzamientos()), antes)

    def test_r1_subconjunto_sin_git_es_infraestructura(self):
        origen = self.origen()
        self.plan([{}])
        antes = len(self.lanzamientos())
        solicitud = Reproduccion('sub', FALLO, 'subconjunto', (2,))
        self.assertIsNone(esperar_resultado(self.adaptador.repetir, CANDIDATO, origen, solicitud, 'd' * 40,
                                               'lote', permitir_lanzar=False))
        with self.assertRaises(ResultadoInvalido) as contexto:
            esperar_resultado(self.adaptador.repetir, CANDIDATO, origen, solicitud, 'd' * 40, 'lote')
        self.assertEqual(contexto.exception.resultado.codigo, 'subconjunto-sin-git')
        self.assertEqual(len(self.lanzamientos()), antes)

    def test_r1_base_transporte_no_envenena_otro_tren(self):
        origen = self.origen()
        nuevo = replace(CANDIDATO, C='e' * 40)
        self.plan([{}])
        ruta, _ = esperar_resultado(self.adaptador.suite, nuevo, 'otro')
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
        ruta, _ = esperar_resultado(self.adaptador.suite, nuevo, 'otro')
        antes = len(self.lanzamientos())
        self.plan([{}] * (antes + 1) + [{'codigo': 75, 'estados': ['falla']}, {}])
        r = self.repetir(origen, 'B')
        self.assertIsInstance(r, Incompleto)
        self.assertIsNone(esperar_resultado(self.adaptador.repetir, nuevo, ruta, Reproduccion('otra', FALLO, 'B'),
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

    def atribuir_con_casos(self, casos_suite, casos_repeticion, *, acreditados=False,
                          campo_duplicado=None, casos_base=None, acreditados_base=None):
        previos = sum(a[2] == 'contract' for a in self.lanzamientos())
        tren = 'casos-' + str(previos)
        candidato = replace(CANDIDATO, B=format(previos + 1, '040x'))
        roja = {'estados': ['falla']}
        repeticion = {'campo_duplicado': campo_duplicado}
        if casos_suite is not SIN_CASOS:
            roja['casos'] = casos_suite
        if casos_repeticion is not SIN_CASOS:
            repeticion['casos'] = casos_repeticion
        base = dict(repeticion)
        if casos_base is SIN_CASOS:
            base.pop('casos', None)
        elif casos_base is not None:
            base['casos'] = casos_base
        self.plan({'contract': [{}] * previos + [roja] + [repeticion] * 2 + [base] * 2})
        origen, suite = esperar_resultado(self.adaptador.suite, candidato, tren)
        self.assertEqual(suite, Completo('rojo', (FALLO,), costo_s=8))

        evidencias = ()
        for objetivo, commit in (('C', candidato.C), ('B', candidato.B)):
            plan = planificar(suite.fallos, (1,), evidencias, 100, absolver=True,
                              firma_distingue_fallo=self.adaptador.firma_distingue_fallo)
            self.assertEqual(plan.siguiente.objetivo, objetivo)
            resultado = esperar_resultado(self.adaptador.repetir, candidato, origen,
                                           plan.siguiente, commit, tren)
            self.assertEqual([i.veredicto for i in resultado.unidades[0].intentos], ['pasa'] * 2)
            esperado = acreditados if objetivo == 'C' or acreditados_base is None else acreditados_base
            self.assertEqual([i.caso_ejecutado for i in resultado.unidades[0].intentos],
                             [esperado] * 2)
            evidencias += (Evidencia(plan.siguiente, resultado),)
        return planificar(suite.fallos, (1,), evidencias, 100, absolver=True,
                          firma_distingue_fallo=self.adaptador.firma_distingue_fallo)

    def test_atribucion_absuelve_si_c_y_b_acreditan_el_caso_fallido(self):
        plan = self.atribuir_con_casos([dict(caso=CASO, resultado='falla')],
                                      [dict(caso=CASO, resultado='pasa')], acreditados=True)
        self.assertTrue(plan.terminado)
        self.assertEqual(plan.incierto, '')
        absolucion, = plan.absoluciones
        self.assertEqual((absolucion.fallo, absolucion.caso), (FALLO, 1))

    def test_atribucion_pide_juicio_si_falta_evidencia_del_caso(self):
        falla = dict(caso=CASO, resultado='falla')
        pasa = dict(caso=CASO, resultado='pasa')
        escenarios = (
            ('omitida', [falla], [dict(caso=CASO, resultado='omitida')]),
            ('recuperada', [falla], [dict(caso=CASO, resultado='recuperada')]),
            ('suite vieja', SIN_CASOS, [pasa]),
            ('repetición vieja', [falla], SIN_CASOS),
            ('sin fallos individuales', [dict(caso=CASO, resultado='recuperada')], [pasa]),
            ('segundo caso ausente', [falla, dict(caso=CASO + ' dos', resultado='falla')], [pasa]),
            ('lista malformada', [falla], {'caso': CASO, 'resultado': 'pasa'}),
            ('identidad vacía', [falla], [dict(caso='', resultado='pasa'), pasa]),
            ('resultado desconocido', [falla], [pasa, dict(caso='otro', resultado='verde')]),
            ('identidad duplicada en la suite', [falla, falla], [pasa]),
            ('identidad duplicada en la repetición', [falla], [pasa, pasa]),
        )
        for escenario, casos_suite, casos_repeticion in escenarios:
            with self.subTest(escenario=escenario):
                plan = self.atribuir_con_casos(casos_suite, casos_repeticion)
                self.assertFalse(plan.terminado)
                self.assertEqual(plan.incierto, 'confirmacion-candidato:' + UNIDAD)
                self.assertEqual(plan.absoluciones, ())

    def test_atribucion_pide_juicio_si_b_no_acredita_el_caso(self):
        falla = dict(caso=CASO, resultado='falla')
        pasa = dict(caso=CASO, resultado='pasa')
        for escenario, casos_base in (('omitida', [dict(caso=CASO, resultado='omitida')]),
                                      ('ausente', [dict(caso='otro', resultado='pasa')]),
                                      ('recibo viejo', SIN_CASOS)):
            with self.subTest(escenario=escenario):
                plan = self.atribuir_con_casos([falla], [pasa], acreditados=True,
                                               casos_base=casos_base, acreditados_base=False)
                self.assertFalse(plan.terminado)
                self.assertEqual(plan.incierto, 'confirmacion-candidato:' + UNIDAD)
                self.assertEqual(plan.absoluciones, ())

    def test_clave_duplicada_dentro_de_un_caso_descarta_evidencia_sin_rechazar(self):
        plan = self.atribuir_con_casos([dict(caso=CASO, resultado='falla')],
                                      [dict(caso=CASO, resultado='pasa', n=1)],
                                      campo_duplicado='n')
        self.assertEqual(plan.incierto, 'confirmacion-candidato:' + UNIDAD)

    def test_campo_duplicado_en_casos_solo_descarta_evidencia(self):
        plan = self.atribuir_con_casos([dict(caso=CASO, resultado='falla')],
                                      [dict(caso=CASO, resultado='pasa')], campo_duplicado='caso')
        self.assertEqual(plan.incierto, 'confirmacion-candidato:' + UNIDAD)

    def test_sin_permiso_no_lanza_y_recupera_recibo_existente(self):
        self.assertIsNone(self.suite(permitir_lanzar=False))
        self.assertEqual(self.lanzamientos(), [])
        original = self.suite()
        self.assertEqual(self.suite(permitir_lanzar=False), original)
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_crash_real_tick_durante_lanzamiento_se_reengancha(self):
        self.plan([{'esperar': True}, {}])
        helper = self.raiz / 'tick.py'
        helper.write_text('import signal, sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) +
            ')\nfrom adaptadores.jale import Jale\nfrom dominio import Candidato\nJale(' +
            repr(str(self.lanzador)) + ').suite(Candidato(' + repr(B) + ', (), ' + repr(C) +
            ', "d" * 40), "lote")\nsignal.pause()\n')
        proceso = subprocess.Popen([sys.executable, str(helper)])
        try:
            limite = time.monotonic() + 10
            while not (self.raiz / 'listo').exists() and time.monotonic() < limite:
                time.sleep(.02)
            self.assertTrue((self.raiz / 'listo').exists())
            from operador import lanzamiento_activo
            registro = Path(self.lanzamientos()[0][1]).parent
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
            self.assertIsNone(proceso.poll())
        finally:
            if proceso.poll() is None:
                proceso.kill()
            proceso.wait(timeout=5)
            (self.raiz / 'seguir').write_text('seguir')
        self.assertEqual(proceso.returncode, -signal.SIGKILL)
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
        self.assertEqual([a[2] for a in self.lanzamientos()], ['e2e'])

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
                while not (self.raiz / 'listo').exists() and time.monotonic() < limite:
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
        self.plan({'contract': [{'codigo': 75, 'sin_linea': True}, {}]})
        _, incompleto = self.suite()
        primero = next(i for i in self.invocaciones() if i['modo'] == 'contract')
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
        transporte = {'transportes': 3, 'codigo_transporte': 127}
        self.plan([transporte])
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
        self.assertEqual(len(self.invocaciones()), 6)

        _, segundo_rechazo = self.suite(reenganche=reenganche)
        self.assertEqual(segundo_rechazo.detalle, 'salida-sin-recibo')
        self.assertNotEqual(segundo_rechazo.evidencia, rechazo.evidencia)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 7)
        contract = [i for i in invocaciones if i['modo'] == 'contract']
        self.assertEqual({(i['log'], i['clave']) for i in contract},
                         {(str(registro / 'corrida.log'), contract[0]['clave'])})
        self.assertEqual(json.loads(codigo.read_text()), 0)
        self.assertEqual(self.suite(reenganche=reenganche)[1], segundo_rechazo)
        self.assertEqual(self.invocaciones(), invocaciones)

        _, resultado = self.suite(reenganche=(2, segundo_rechazo.evidencia))
        self.assertEqual(resultado, Completo('verde', costo_s=8))
        self.assertEqual(len(self.lanzamientos()), 2)
        self.assertEqual(self.invocaciones()[-1]['clave'],
                         next(i['clave'] for i in invocaciones if i['modo'] == 'e2e'))
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
        # Entra otro cambio del lanzador antes de que el tick siguiente recupere el reenganche.
        posterior = self.raiz / 'snapshot-posterior.sh'
        posterior.write_text(self.lanzador.read_text())
        vigente.write_text('#!/bin/bash\nexit 64\n')
        operador.lanzador.return_value = str(posterior)
        _, resultado = self.suite(reenganche=reenganche)
        self.assertEqual(json.loads(solicitud.read_text()), [argv[0], str(posterior), *argv[2:]])
        operador.validar_guardado.assert_called_with(str(posterior))
        self.assertEqual(operador.lanzador.call_count, 3)
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        self.assertEqual((registro / 'iniciado.json').read_bytes(), iniciado)
        self.assertEqual(json.loads((registro / 'codigo-reenganche-1.json').read_text()), 127)
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 0)
        invocaciones = self.invocaciones()
        self.assertEqual(len(invocaciones), 4)
        self.assertEqual({Path(i['log']).parent.name for i in invocaciones}, {'0'})
        self.assertEqual({i['clave'] for i in invocaciones}, {json.loads(iniciado)['clave']})
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_sen7279_64_dentro_de_un_reenganche_no_autoriza_otra_clave(self):
        from unittest.mock import Mock
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        bueno = self.raiz / 'snapshot-bueno.sh'
        rechaza = self.raiz / 'snapshot-rechaza.sh'
        bueno.write_text(self.lanzador.read_text())
        rechaza.write_text('#!/bin/bash\nexit 64\n')
        operador = Mock(checkout=self.raiz)
        operador.lanzador.return_value = str(bueno)
        operador.entorno_lanzador.return_value = None
        self.adaptador.operador = operador
        self.plan([{'transportes': 3, 'codigo_transporte': 127}])
        _, rechazo = self.suite()
        self.assertEqual(rechazo.detalle, 'salida-sin-recibo')
        registro = Path(rechazo.evidencia).parent
        clave = json.loads((registro / 'iniciado.json').read_text())['clave']
        operador.lanzador.return_value = str(rechaza)
        _, segundo = self.suite(reenganche=(1, rechazo.evidencia))
        self.assertEqual(segundo.detalle, 'salida-sin-recibo')
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 64)
        operador.lanzador.return_value = str(bueno)
        _, resultado = self.suite(reenganche=(2, rechazo.evidencia))
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        self.assertEqual(json.loads((registro / 'codigo-reenganche-2.json').read_text()), 64)
        invocaciones = self.invocaciones()
        self.assertEqual({Path(i['log']).parent.name for i in invocaciones}, {'0'})
        self.assertEqual({i['clave'] for i in invocaciones}, {clave})
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
        otro = esperar_resultado(self.adaptador.repetir, CANDIDATO, origen, Reproduccion('otra', FALLO, 'B'),
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
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
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
        self.plan({'contract': [{'codigo': 125, 'sin_linea': True}, {}]})
        recibo = self.suite()
        self.assertEqual(self.suite(), recibo)
        self.assertEqual(len(self.invocaciones()), 2)
        registro = Path(next(i['log'] for i in self.invocaciones() if i['modo'] == 'contract')).parent
        self.assertEqual(recibo[1], RechazoLanzador(str(registro / 'coordinador.log'),
                                                  'salida-sin-recibo'))
        self.assertEqual(json.loads((registro / 'codigo.json').read_text()), 125)
        self.assertEqual(self.suite(intento=1)[1].veredicto, 'verde')
        self.assertEqual(len(self.invocaciones()), 3)
        self.assertEqual(len({i['clave'] for i in self.invocaciones()}), 3)

    def test_boat_ejecuta_ambos_modos_en_paralelo_y_agrega_el_fallback(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', boat_esperar_s=42)
        self.plan({'contract': [{'barrera': True, 'fallback': True,
                                 'estados': ['falla', 'no-corrida']}],
                   'e2e': [{'barrera': True, 'estados': ['falla']}]})
        with patch.dict(os.environ, {'REMOTE_RUN_CLAVE': 'clave-heredada',
                                     'SEN_E2E_FRAGMENTO': '1/2', 'TURBO_FORCE': 'false'}):
            ruta, resultado = self.suite()
        e2e = Fallo('apps/web/e2e/a.spec.ts', FIRMA, 'solo-candidato', 'e2e', 'e2e', 'jale', 4)
        self.assertEqual(resultado, Completo('rojo', (FALLO, e2e), 8, (OTRA,)))
        self.assertEqual([r['codigo'] for r in json.loads(Path(ruta).read_text())], [1, 1])
        invocaciones = self.invocaciones()
        self.assertCountEqual([(i['host'], i['modo']) for i in invocaciones],
                             [('boat', 'contract'), ('boat', 'e2e')])
        for i in invocaciones:
            self.assertTrue((self.raiz / ('cruzado-' + i['modo'])).exists())
            variables = dict(i['boat'])
            self.assertRegex(variables.pop('SEN_RUN_ID'), r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
            self.assertEqual(variables, {'REMOTE_RUN_CLAVE': None, 'SEN_E2E_FRAGMENTO': None,
                'SEN_RUN_OWNER': 'pao-tren', 'SEN_BOAT_PRIORIDAD': 'tren',
                'SEN_BOAT_ESPERAR': '42', 'TURBO_FORCE': 'true'})
            encabezado = ('HOST=desktop fallback=sin-lugar' if i['modo'] == 'contract'
                          else 'HOST=boat fallback=none')
            self.assertIn(encabezado, Path(i['log']).read_text())
        self.assertNotEqual(*[i['boat']['SEN_RUN_ID'] for i in invocaciones])

    def test_boat_cero_con_recibo_ignora_omisiones_de_tipos(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'cobertura': 'PARTIAL: boat types-omitted=2\n'}])
        ruta, resultado = self.suite()
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        self.assertEqual(json.loads(Path(ruta).read_text())[0]['codigo'], 0)
        self.assertEqual(self.invocaciones()[0]['boat']['SEN_BOAT_ESPERAR'], '180')

    def test_boat_adapta_espera_a_la_salud_del_desk_y_guarda_el_motivo(self):
        from adaptadores import seleccionar
        casos = (
            ('running', '14.00', None, True, 'desk-sano'),
            ('paused', '3.00', None, False, 'grupo-en-pausa'),
            ('running', '14.01', None, False, 'carga-alta'),
            ('running', '3.00', OSError(), False, 'sonda-fallida'),
            ('running', '3.00', subprocess.TimeoutExpired('ssh', 6), False, 'plazo-agotado'),
            ('desconocido', '3.00', None, False, 'grupo-ilegible'),
        )
        for estado_grupo, carga, error, sano, motivo in casos:
            with self.subTest(motivo=motivo):
                self.adaptador = seleccionar({'adaptador': 'jale', 'lanzador': str(self.lanzador),
                    'host': 'boat', 'modos': ['contract'], 'boat_esperar_no_sano_s': 900,
                    'desk_host': 'desk-de-prueba', 'desk_grupos': ['pesadas', 'contratos']}, None)
                sonda = Mock(return_value=(f'Group "pesadas" (3 parallel): {estado_grupo}\n'
                                           'Group "contratos" (1 parallel): running\n'
                                           f'{carga} 1.00 1.00 1/2 3\n14\n'), side_effect=error)
                with patch.object(self.adaptador, 'sonda_desk', sonda):
                    self.assertEqual(self.suite(tren=motivo)[1].veredicto, 'verde')
                invocacion = self.invocaciones()[-1]
                registro = Path(invocacion['log']).parent
                espera = 180 if sano else 900
                self.assertEqual(invocacion['boat']['SEN_BOAT_ESPERAR'], str(espera))
                entorno = json.loads((registro / 'entorno.json').read_text())
                self.assertEqual(entorno['SEN_BOAT_ESPERAR'], str(espera))
                salud = json.loads((registro / 'salud-desk.json').read_text())
                self.assertEqual((salud['sano'], salud['motivo'], salud['espera_s']),
                                 (sano, motivo, espera))
                self.assertEqual(salud['carga_1m'], None if error else float(carga))
                sonda.assert_called_once()
                argv, = sonda.call_args.args
                self.assertIn('desk-de-prueba', argv)
                self.assertIn('BatchMode=yes', argv)
                self.assertNotIn('--json', argv[-1])
                self.assertLessEqual(sonda.call_args.kwargs['timeout'], 8)
        self.assertEqual(len(self.invocaciones()), len(casos))

    def test_boat_sondea_grupos_en_ejecucion_sin_tareas(self):
        def pueue(argv, **opciones):
            grupos = ('Group "pesadas" (3 parallel): running\n'
                      'Group "jale" (7 parallel): running\n')
            # `status` omite las dos cabeceras si los grupos están vacíos.
            cabeceras = grupos if 'pueue group' in argv[-1] else ''
            return cabeceras + '2.00 1.00 1.00 1/2 3\n14\n'

        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',),
                                   boat_esperar_no_sano_s=900, sonda_desk=pueue)
        self.assertEqual(self.suite()[1].veredicto, 'verde')
        invocacion, = self.invocaciones()
        self.assertEqual(invocacion['boat']['SEN_BOAT_ESPERAR'], '180')
        decision = Path(invocacion['log']).with_name('salud-desk.json')
        self.assertEqual(json.loads(decision.read_text())['motivo'], 'desk-sano')

    def test_boat_sondea_solo_con_la_regla_activa_y_en_boat(self):
        for host, opciones in (('boat', {}), ('desktop', {'boat_esperar_no_sano_s': 900})):
            with self.subTest(host=host):
                sonda = Mock()
                self.adaptador = self.Jale(self.lanzador, host=host, modos=('contract',),
                                          sonda_desk=sonda, **opciones)
                self.assertEqual(self.suite(tren=host)[1].veredicto, 'verde')
                sonda.assert_not_called()
                registro = Path(self.invocaciones()[-1]['log']).parent
                entorno = json.loads((registro / 'entorno.json').read_text())
                self.assertEqual(entorno.get('SEN_BOAT_ESPERAR'), '180' if host == 'boat' else None)
                self.assertFalse((registro / 'salud-desk.json').exists())

    def test_boat_recupera_el_entorno_de_un_prelanzamiento_sin_sondear(self):
        sonda = Mock(side_effect=OSError())
        opciones = dict(host='boat', modos=('contract',), prelanzar=True, sonda_desk=sonda)
        self.adaptador = self.Jale(self.lanzador, boat_esperar_no_sano_s=900, **opciones)
        tren = 'prelanzado'
        registro = self.adaptador.estado / clave('suite', tren) / 'contract' / '0'
        registro.mkdir(parents=True)
        entorno = registro / 'entorno.json'
        contenido = json.dumps({'SEN_BOAT_ESPERAR': '420', 'SEN_RUN_ID': 'clave-guardada',
                                'SEN_RUN_OWNER': 'pao-tren', 'REMOTE_RUN_CLAVE': None})
        entorno.write_text(contenido)

        self.adaptador.prelanzar(CANDIDATO, tren)
        self.assertEqual(self.suite(tren=tren)[1].veredicto, 'verde')
        sonda.assert_not_called()
        self.assertEqual(entorno.read_text(), contenido)
        self.assertEqual(self.invocaciones()[0]['boat']['SEN_BOAT_ESPERAR'], '420')

    def test_boat_cambio_a_desktop_tras_guardar_solo_el_entorno_recupera_la_clave(self):
        from adaptadores.jale import guardar

        def interrumpir(ruta, valor):
            if ruta.name == 'solicitud.json':
                raise InterruptedError
            guardar(ruta, valor)

        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        registro = self.adaptador.estado / clave('suite', 'lote') / 'contract' / '0'
        with patch('adaptadores.jale.guardar', side_effect=interrumpir):
            with self.assertRaises(InterruptedError):
                self.suite()
        entorno = registro / 'entorno.json'
        self.assertIn('SEN_RUN_ID', json.loads(entorno.read_text()))
        self.assertFalse((registro / 'solicitud.json').exists())

        self.adaptador = self.Jale(self.lanzador, host='desktop', modos=('contract',))
        self.assertEqual(self.suite()[1].veredicto, 'verde')
        invocacion, = self.invocaciones()
        self.assertEqual(invocacion['host'], 'desktop')
        self.assertEqual(json.loads(entorno.read_text()), {})
        self.assertRegex(invocacion['clave'], r'^pao-jale-[0-9a-f]{64}$')

    def test_boat_75_pendiente_conserva_la_decision_entre_ticks(self):
        sana = ('Group "pesadas" (3 parallel): running\n'
                'Group "jale" (7 parallel): running\n'
                '2.00 1.00 1.00 1/2 3\n14\n')
        sonda = Mock(side_effect=[sana.replace('2.00', '15.00'), sana])
        opciones = dict(host='boat', modos=('contract',), boat_esperar_no_sano_s=900,
                        sonda_desk=sonda)
        self.adaptador = self.Jale(self.lanzador, **opciones)
        self.plan([{'pendiente': 'intento desconocido'}, {}])
        raiz, incompleto = self.suite()
        self.assertEqual(incompleto, Incompleto(75, raiz))

        self.adaptador = self.Jale(self.lanzador, **opciones)
        self.assertEqual(self.suite(intento=1, reanudar=incompleto.reanudar)[1].veredicto, 'verde')
        sonda.assert_called_once()
        primera, segunda = self.invocaciones()
        self.assertEqual(primera['boat']['SEN_RUN_ID'], segunda['boat']['SEN_RUN_ID'])
        self.assertEqual([i['boat']['SEN_BOAT_ESPERAR'] for i in (primera, segunda)],
                         ['900', '900'])
        decisiones = [json.loads(Path(i['log']).with_name('salud-desk.json').read_text())
                      for i in (primera, segunda)]
        self.assertEqual(decisiones[0], decisiones[1])
        self.assertEqual(decisiones[0]['motivo'], 'carga-alta')

    def test_boat_75_pendiente_sin_sonda_usa_la_espera_vigente(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',),
                                   boat_esperar_s=180)
        self.plan([{'pendiente': 'intento desconocido'}, {}])
        raiz, incompleto = self.suite()
        self.assertEqual(incompleto, Incompleto(75, raiz))

        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',),
                                   boat_esperar_s=42)
        self.assertEqual(self.suite(intento=1, reanudar=incompleto.reanudar)[1].veredicto, 'verde')
        primera, segunda = self.invocaciones()
        self.assertEqual(primera['boat']['SEN_RUN_ID'], segunda['boat']['SEN_RUN_ID'])
        self.assertEqual([i['boat']['SEN_BOAT_ESPERAR'] for i in (primera, segunda)],
                         ['180', '42'])

    def test_boat_dos_modos_comparten_una_sonda_con_plazo_total(self):
        ssh = self.raiz / 'ssh'
        ssh.write_text('#!/bin/sh\nexec sleep 60\n')
        ssh.chmod(0o700)
        sonda = Mock(wraps=subprocess.check_output)
        self.adaptador = self.Jale(self.lanzador, host='boat', boat_esperar_no_sano_s=900,
                                   sonda_desk=sonda)
        with patch.dict(os.environ, {'PATH': str(self.raiz) + os.pathsep + os.environ['PATH']}):
            inicio = time.monotonic()
            self.adaptador.suite(CANDIDATO, 'lote')
            self.assertLessEqual(time.monotonic() - inicio, 8)
            self.assertEqual(self.suite()[1].veredicto, 'verde')
        sonda.assert_called_once()
        self.assertCountEqual([i['modo'] for i in self.invocaciones()], ['contract', 'e2e'])
        for i in self.invocaciones():
            self.assertEqual(i['boat']['SEN_BOAT_ESPERAR'], '900')
            decision = Path(i['log']).with_name('salud-desk.json')
            self.assertEqual(json.loads(decision.read_text())['motivo'], 'plazo-agotado')

    def test_boat_repeticiones_y_adelantos_comparten_la_sonda_del_tick(self):
        self.adaptador = self.Jale(self.lanzador, modos=('contract',))
        origen = self.origen()
        self.plan([{}])
        sonda = Mock(side_effect=OSError)
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',),
                                   repeticiones_paralelas=3, boat_esperar_no_sano_s=900,
                                   sonda_desk=sonda)
        solicitud = Reproduccion('solicitud-B', FALLO, 'B')
        self.adaptador.adelantar_repeticiones(CANDIDATO, origen, [(solicitud, B)], 'lote')
        self.repetir(origen)
        self.repetir(origen, objetivo='B')
        sonda.assert_called_once()
        self.assertEqual([i['boat']['SEN_BOAT_ESPERAR'] for i in self.invocaciones()[1:]],
                         ['900'] * 4)

        self.adaptador.operador = Mock(checkout=None)
        self.adaptador.operador.entorno_lanzador.return_value = None
        self.assertEqual(self.suite(tren='lote-nuevo')[1].veredicto, 'verde')
        self.assertEqual(sonda.call_count, 2)

    def test_boat_reenganche_conserva_la_espera_del_prelanzamiento(self):
        sonda = Mock(side_effect=OSError())
        opciones = dict(host='boat', modos=('contract',), prelanzar=True, sonda_desk=sonda)
        self.adaptador = self.Jale(self.lanzador, boat_esperar_no_sano_s=900, **opciones)
        self.plan([{'transportes': 3, 'codigo_transporte': 127}])
        self.adaptador.prelanzar(CANDIDATO, 'lote')
        _, rechazo = self.suite()
        registro = Path(rechazo.evidencia).parent
        entorno = registro / 'entorno.json'
        contenido = entorno.read_bytes()
        self.adaptador = self.Jale(self.lanzador, boat_esperar_no_sano_s=600, **opciones)
        self.assertEqual(self.suite(reenganche=(1, rechazo.evidencia))[1].veredicto, 'verde')
        sonda.assert_called_once()
        self.assertEqual(entorno.read_bytes(), contenido)
        self.assertEqual([i['boat']['SEN_BOAT_ESPERAR'] for i in self.invocaciones()], ['900'] * 4)

    def test_boat_75_tras_corte_de_red_respeta_el_cambio_de_host_y_la_cache(self):
        self.plan({'contract': [{'pendiente': 'resultado o transporte no acreditado'}, {}] * 2})
        for host in ('auto', 'desktop'):
            with self.subTest(host=host):
                antes = len(self.invocaciones())
                self.adaptador = self.Jale(self.lanzador, host='boat')
                raiz, resultado = self.suite(tren=host)
                self.assertEqual(resultado, Incompleto(75, raiz))
                self.assertEqual(json.loads((Path(raiz) / 'e2e' / 'completo.json').read_text()), 0)
                primera = next(i for i in self.invocaciones()[antes:] if i['modo'] == 'contract')
                log = Path(primera['log'])
                self.assertFalse(log.exists())
                self.assertTrue(log.with_suffix('.log.boat-pendiente').exists())
                self.assertEqual(self.suite(tren=host), (raiz, resultado))
                self.assertEqual(len(self.invocaciones()), antes + 2)

                self.adaptador = self.Jale(self.lanzador, host={'contract': host, 'e2e': 'boat'})
                _, final = self.suite(tren=host, intento=1, reanudar=raiz)
                self.assertEqual(final, Completo('verde', costo_s=8))
                segunda, = self.invocaciones()[antes + 2:]
                self.assertEqual((segunda['modo'], segunda['host']), ('contract', host))
                self.assertRegex(segunda['clave'], r'^pao-jale-[0-9a-f]{64}$')
                self.assertIsNone(segunda['boat']['SEN_RUN_ID'])
                self.assertNotEqual(segunda['log'], primera['log'])
                self.assertEqual(len(self.lanzamientos()), antes + 3)

    def test_boat_tres_intentos_pendientes_reenganchan_la_misma_corrida(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [{'pendiente': 'intento desconocido', 'pendientes': 3}]})
        reanudar = ''
        for intento in range(3):
            raiz, resultado = self.suite(intento=intento, reanudar=reanudar)
            self.assertEqual(resultado, Incompleto(75, raiz))
            self.assertTrue((Path(raiz) / 'contract' / str(intento) /
                             'corrida.log.boat-pendiente').is_file())
            reanudar = resultado.reanudar
        self.assertEqual(self.suite(intento=3, reanudar=reanudar)[1], Completo('verde', costo_s=8))
        contratos = [i for i in self.invocaciones() if i['modo'] == 'contract']
        self.assertEqual([Path(i['log']).parent.name for i in contratos], ['0', '1', '2', '3'])
        identidad = contratos[0]['boat']['SEN_RUN_ID']
        for i in contratos:
            entorno = Path(i['log']).with_name('entorno.json')
            self.assertEqual(json.loads(entorno.read_text())['SEN_RUN_ID'], identidad)
            self.assertEqual(i['boat']['SEN_RUN_ID'], identidad)
        self.assertEqual(sum(i['modo'] == 'e2e' for i in self.invocaciones()), 1)
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def test_boat_255_reintenta_transporte_con_la_misma_identidad(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'transportes': 1}])
        self.assertEqual(self.suite()[1], Completo('verde', costo_s=4))
        primera, segunda = self.invocaciones()
        self.assertEqual((primera['log'], primera['boat']), (segunda['log'], segunda['boat']))
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_boat_75_del_fallback_sin_recibo_recupera_el_envio_al_desk(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'fallback': True, 'fallback_75': True}])
        raiz, resultado = self.suite()
        self.assertEqual(resultado, Incompleto(75, raiz))
        primera, = self.invocaciones()
        self.assertIn('HOST=desktop fallback=sin-lugar', Path(primera['log']).read_text())
        self.assertEqual(self.suite(intento=1, reanudar=raiz)[1], Completo('verde', costo_s=4))
        self.assertEqual(self.invocaciones()[-1]['boat']['SEN_RUN_ID'],
                         primera['boat']['SEN_RUN_ID'])
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_boat_75_del_fallback_con_log_no_utf8_conserva_identidad(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'fallback': True, 'fallback_75': True, 'log_no_utf8': True}])
        raiz, resultado = self.suite()
        self.assertEqual(resultado, Incompleto(75, raiz))
        primera, = self.invocaciones()
        self.assertIn(b'\xff', Path(primera['log']).read_bytes())
        self.assertEqual(self.suite(intento=1, reanudar=raiz)[1], Completo('verde', costo_s=4))
        segunda = self.invocaciones()[-1]
        self.assertEqual(segunda['boat']['SEN_RUN_ID'], primera['boat']['SEN_RUN_ID'])
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_boat_prioriza_reenganche_incierto_sobre_rechazo_terminal(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [{'codigo': 64, 'sin_linea': True}, {}],
                   'e2e': [{'transportes': 3}]})
        _, incierto = self.suite()
        self.assertEqual(incierto.detalle, 'salida-sin-recibo')
        _, rechazo = self.suite(reenganche=(1, incierto.evidencia))
        self.assertEqual(rechazo.detalle, 'lanzador-rechazo')
        self.assertEqual(self.suite(intento=1)[1], Completo('verde', costo_s=8))
        self.assertCountEqual([l[2] for l in self.lanzamientos()], ['contract', 'contract', 'e2e'])
        self.assertEqual(len({i['boat']['SEN_RUN_ID'] for i in self.invocaciones()
                              if i['modo'] == 'e2e'}), 1)

    def test_boat_75_no_reutiliza_identidad_irrecuperable(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        causas = ('intento interrupted', 'resultado desconocido tras el retiro',
                  'cancelación sin resultado acreditado', 'despacho sin VM registrada en la corrida')
        for n, causa in enumerate(causas):
            with self.subTest(causa=causa):
                tren = 'lote-' + str(n)
                antes = len(self.lanzamientos())
                self.plan([{}] * antes + [{'pendiente': causa, 'pendiente_fijo': True}, {}])
                raiz, resultado = self.suite(tren=tren)
                self.assertEqual(resultado, Incompleto(75, raiz))
                self.assertEqual(self.suite(tren=tren, intento=1, reanudar=raiz)[1],
                                 Completo('verde', costo_s=4))
                primera, segunda = self.invocaciones()[-2:]
                self.assertNotEqual(primera['boat']['SEN_RUN_ID'], segunda['boat']['SEN_RUN_ID'])
                self.assertEqual(len(self.lanzamientos()), antes + 2)

    def test_boat_125_interrumpido_reintenta_con_identidad_nueva(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'fallback': True, 'codigo': 125, 'sin_linea': True, 'interrumpida': True}, {}])
        _, rechazo = self.suite()
        self.assertIsInstance(rechazo, RechazoLanzador)
        self.assertEqual(rechazo.detalle, 'corrida-interrumpida')
        self.assertEqual(self.suite(intento=1)[1], Completo('verde', costo_s=4))
        primera, segunda = self.invocaciones()
        self.assertNotEqual(primera['boat']['SEN_RUN_ID'], segunda['boat']['SEN_RUN_ID'])

    def verificar_boat_terminal_sin_recibo(self, plan):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'e2e': [plan, {}]})
        inicial = estado()
        tren = Tren('lote', (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO))
        inicial = replace(inicial, trenes=(tren,))
        obs = observacion(ci_candidato='verde')
        git = object()
        efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
        llegada = esperar_resultado(ejecutar, efecto, inicial, obs, git, self.adaptador)
        self.assertTrue(llegada.completo)
        pausado, _ = paso(inicial, llegada.observacion)
        primera = next(i for i in self.invocaciones() if i['modo'] == 'e2e')
        log = Path(primera['log'])
        pausa = Pausa('infraestructura', detalle='salida-sin-recibo',
                      archivos=(str(log.with_name('coordinador.log')),))
        self.assertEqual(pausado.pausa, pausa)

        decision = Decision('reenganchar', inicial.repo, tren.id, 'reanudar', None,
                            'consultar la corrida', obs.ahora, 'pausa-1', pausa)
        reanudado, (_, reanudar) = paso(pausado, replace(obs, decision=decision, pausa_id='pausa-1'))
        self.assertEqual(intento_suite(reanudado.trenes[0].fase), 0)
        llegada = esperar_resultado(ejecutar, reanudar, reanudado, obs, git, self.adaptador)
        self.assertTrue(llegada.completo)
        pausado, _ = paso(reanudado, llegada.observacion)
        self.assertEqual(pausado.pausa, pausa)
        segunda, = self.invocaciones()[2:]
        self.assertEqual((segunda['log'], segunda['clave']), (primera['log'], primera['clave']))
        self.assertEqual(json.loads(log.with_name('codigo-reenganche-1.json').read_text()),
                         plan['codigo'])
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

        decision = Decision('nuevo-intento', inicial.repo, tren.id, 'absolver', None,
                            'la corrida terminó', obs.ahora, 'pausa-2', pausa)
        absuelto, (_, reintentar) = paso(pausado, replace(obs, decision=decision, pausa_id='pausa-2'))
        self.assertIsNone(absuelto.pausa)
        fase = absuelto.trenes[0].fase
        self.assertEqual(intento_suite(fase), 1)
        self.assertEqual((fase.reenganches, fase.reanudaciones), (0, 0))
        self.assertEqual(reintentar.tipo, 'reanudar-suite')
        llegada = esperar_resultado(ejecutar, reintentar, absuelto, obs, git, self.adaptador)
        final, _ = paso(absuelto, llegada.observacion)
        self.assertIsInstance(final.trenes[0].fase, Certificado)
        tercera, = self.invocaciones()[3:]
        self.assertEqual(tercera['modo'], 'e2e')
        self.assertEqual([log.parent.name, Path(tercera['log']).parent.name], ['0', '1'])
        self.assertNotEqual(primera['clave'], tercera['clave'])
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e', 'e2e'])
        return log

    def test_boat_terminal_sin_recibo_reengancha_y_absolver_abre_otro_intento(self):
        self.verificar_boat_terminal_sin_recibo({'codigo': 1, 'sin_linea': True, 'terminal': True})

    def test_boat_fallback_cortado_por_pueue_reengancha_y_absolver_abre_otro_intento(self):
        log = self.verificar_boat_terminal_sin_recibo({
            'codigo': 124, 'codigo_suite': 143, 'sin_linea': True,
            'terminal': True, 'fallback': True, 'aviso': True})
        self.assertIn('RESULT mode=e2e exit=143\nEXIT=143\nPUEUE_EXIT=124\n',
                      log.read_text())

    def test_boat_fallback_con_tarea_viva_reengancha_sin_duplicar(self):
        from test_hasito import GitFalso, verificar_rechazo
        self.git = GitFalso(self.raiz)
        self.adaptador = self.Jale(self.lanzador, host='boat')

        def configurar(respuesta):
            if respuesta == '127':
                self.plan({'e2e': [{'transportes': 3, 'codigo_transporte': 127, 'fallback': True}]})
            else:
                e2e = [i for i in self.invocaciones() if i['modo'] == 'e2e']
                self.assertEqual(len(e2e), 3)
                self.assertIn('HOST=desktop fallback=sin-lugar run_id=',
                              Path(e2e[0]['log']).read_text())

        verificar_rechazo(self, 'iniciar-suite', configurar, codigo=127)
        e2e = [i for i in self.invocaciones() if i['modo'] == 'e2e']
        self.assertEqual(len(e2e), 4)
        self.assertEqual(len({(i['log'], i['clave']) for i in e2e}), 1)
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def test_boat_salida_2_sin_publicacion_reengancha_la_misma_identidad(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('e2e',))
        self.plan([{'transportes': 1, 'codigo_transporte': 2}])
        _, rechazo = self.suite()
        primera, = self.invocaciones()
        log = Path(primera['log'])
        self.assertFalse(log.exists())
        self.assertEqual(json.loads(log.with_name('codigo.json').read_text()), 2)
        self.assertEqual(rechazo, RechazoLanzador(str(log.with_name('coordinador.log')),
                                                'salida-sin-recibo'))

        _, resultado = self.suite(reenganche=(1, rechazo.evidencia))
        self.assertEqual(resultado, Completo('verde', costo_s=4))
        segunda = self.invocaciones()[-1]
        self.assertEqual((segunda['log'], segunda['boat']['SEN_RUN_ID']),
                         (primera['log'], primera['boat']['SEN_RUN_ID']))
        self.assertEqual(len(self.lanzamientos()), 1)

    def test_desktop_sin_recibo_absolver_abre_otro_registro_y_clave(self):
        from test_hasito import GitFalso, verificar_rechazo
        self.git = GitFalso(self.raiz)
        self.adaptador = self.Jale(self.lanzador, host='desktop')

        def configurar(_):
            self.plan({'e2e': [{'codigo': 1, 'sin_linea': True, 'terminal': True}, {}]})

        verificar_rechazo(self, 'iniciar-suite', configurar, codigo=1, decision='absolver')
        primera, segunda = [i for i in self.invocaciones() if i['modo'] == 'e2e']
        self.assertEqual([Path(i['log']).parent.name for i in (primera, segunda)], ['0', '1'])
        self.assertNotEqual(primera['clave'], segunda['clave'])
        self.assertEqual(Path(primera['log']).read_text(), 'RESULT mode=e2e exit=1\nEXIT=1\n')
        self.assertCountEqual([i['modo'] for i in self.invocaciones()], ['contract', 'e2e', 'e2e'])

    def test_boat_rechaza_un_recibo_con_bytes_no_utf8(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [{'recibo_no_utf8': True}]})
        _, resultado = self.suite()
        contract = next(i for i in self.invocaciones() if i['modo'] == 'contract')
        self.assertEqual(resultado, RechazoLanzador(contract['log'], 'recibo-malformado'))
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def test_boat_fallback_sin_recibo_admite_bytes_no_utf8_segun_host_guardado(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        self.plan([{'codigo': 1, 'sin_linea': True, 'fallback': True, 'log_no_utf8': True}])
        raiz, resultado = self.suite()
        primera, = self.invocaciones()
        log = Path(primera['log'])
        self.assertIn(b'\xff', log.read_bytes())
        self.assertEqual(resultado, RechazoLanzador(str(log.with_name('coordinador.log')),
                                                  'salida-sin-recibo'))

        self.adaptador = self.Jale(self.lanzador, host='desktop', modos=('contract',))
        self.assertEqual(self.suite(), (raiz, resultado))
        self.assertEqual(self.invocaciones(), [primera])

    def test_boat_cobertura_parcial_pausa_el_tren_y_reanuda_en_otro_intento(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [
            {'cobertura': 'PARTIAL: boat types-omitted=2\nPARTIAL: boat omitted=1\n'}, {}]})
        inicial = estado()
        tren = Tren('lote', (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO))
        inicial = replace(inicial, trenes=(tren,))
        obs = observacion(ci_candidato='verde')
        git = object()
        efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
        llegada = esperar_resultado(ejecutar, efecto, inicial, obs, git, self.adaptador)
        self.assertTrue(llegada.completo)
        pausado, efectos = paso(inicial, llegada.observacion)
        primera = next(i for i in self.invocaciones() if i['modo'] == 'contract')
        self.assertEqual(pausado.pausa, Pausa('infraestructura', detalle='cobertura-parcial-boat',
                                             archivos=(primera['log'],)))
        self.assertEqual(efectos, ())
        self.assertIn('PARTIAL: boat omitted=1', Path(primera['log']).read_text())

        decision = Decision('reanudar-cobertura', inicial.repo, tren.id, 'reanudar', None,
                            'cobertura corregida', obs.ahora, 'pausa-1', pausado.pausa)
        reanudado, (_, reanudar) = paso(pausado, replace(obs, decision=decision, pausa_id='pausa-1'))
        self.assertIsNone(reanudado.pausa)
        self.assertEqual(reanudar.tipo, 'reanudar-suite')
        fase = reanudado.trenes[0].fase
        self.assertEqual(intento_suite(fase), 1)
        self.assertEqual((fase.reenganches, fase.reanudaciones), (0, 0))
        llegada = esperar_resultado(ejecutar, reanudar, reanudado, obs, git, self.adaptador)
        final, _ = paso(reanudado, llegada.observacion)
        self.assertIsInstance(final.trenes[0].fase, Certificado)
        segunda, = [i for i in self.invocaciones()[2:] if i['modo'] == 'contract']
        self.assertEqual(Path(segunda['log']).parent.name, '1')
        self.assertNotEqual(segunda['boat']['SEN_RUN_ID'], primera['boat']['SEN_RUN_ID'])
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'contract', 'e2e'])

    def test_boat_cobertura_parcial_prevalece_sobre_recibo_invalido_del_otro_modo(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [{'malformado': True}],
                   'e2e': [{'cobertura': 'PARTIAL: boat omitted=1\n'}]})
        _, resultado = self.suite()
        e2e = next(i for i in self.invocaciones() if i['modo'] == 'e2e')
        self.assertEqual(resultado, RechazoLanzador(e2e['log'], 'cobertura-parcial-boat'))

    def test_boat_sin_recibo_pausa_aunque_el_otro_modo_tenga_recibo_invalido(self):
        from test_hasito import GitFalso, verificar_rechazo
        self.git = GitFalso(self.raiz)
        self.adaptador = self.Jale(self.lanzador, host='boat')

        def configurar(_):
            self.plan({'contract': [{'malformado': True}],
                       'e2e': [{'codigo': 1, 'sin_linea': True}]})

        verificar_rechazo(self, 'iniciar-suite', configurar, codigo=1, detalle='salida-sin-recibo',
                          decision='abortar')

    def test_boat_coordinador_interrumpido_prevalece_y_recupera_el_mismo_intento(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan({'contract': [{'cobertura': 'PARTIAL: boat omitted=1\n'}]})
        raiz = self.adaptador.estado / clave('suite', 'lote')
        registro = raiz / 'e2e' / '0'

        recoger = self.adaptador.recoger

        def recoger_sin_codigo_de_e2e(ruta, proceso, *args):
            if ruta == registro:
                proceso.wait(timeout=10)
                (registro / 'codigo.json').unlink()
            return recoger(ruta, proceso, *args)

        with patch.object(self.adaptador, 'recoger', side_effect=recoger_sin_codigo_de_e2e):
            with self.assertRaises(ResultadoInvalido) as contexto:
                self.suite()
        self.assertEqual(contexto.exception.resultado.codigo, 'corrida-interrumpida')
        primera = next(i for i in self.invocaciones() if i['modo'] == 'e2e')
        self.assertEqual(json.loads((registro / 'iniciado.json').read_text()),
                         {'clave': primera['boat']['SEN_RUN_ID']})

        _, resultado = self.suite(permitir_lanzar=False)
        self.assertEqual(resultado, RechazoLanzador(str(raiz / 'contract' / '0' / 'corrida.log'),
                                                    'cobertura-parcial-boat'))
        segunda = self.invocaciones()[-1]
        self.assertEqual((segunda['log'], segunda['boat']['SEN_RUN_ID']),
                         (primera['log'], primera['boat']['SEN_RUN_ID']))
        self.assertCountEqual([a[2] for a in self.lanzamientos()], ['contract', 'e2e'])

    def test_boat_tick_muerto_recupera_los_dos_coordinadores_vivos(self):
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.plan([{'barrera': True, 'esperar': True}])
        arbol = str(Path(__file__).resolve().parents[1])
        script = f'''
import sys
sys.path.insert(0, {arbol!r})
from adaptadores.jale import Jale
from dominio import Candidato
Jale({str(self.lanzador)!r}, host='boat').suite(Candidato({B!r}, (), {C!r}, 'd' * 40), 'lote')
'''
        proceso = subprocess.Popen([sys.executable, '-I', '-c', script])
        try:
            juntos = [self.raiz / ('cruzado-' + modo) for modo in ('contract', 'e2e')]
            limite = time.monotonic() + 10
            while not all(r.exists() for r in juntos) and time.monotonic() < limite:
                time.sleep(.02)
            self.assertTrue(all(r.exists() for r in juntos))
            procesos = self.invocaciones()
            self.assertEqual(len(procesos), 2)
            self.assertTrue(all(not Path(i['log']).with_name('codigo.json').exists()
                                for i in procesos))
        finally:
            if proceso.poll() is None:
                proceso.kill()
            proceso.wait(timeout=5)
            (self.raiz / 'seguir').touch()
        self.assertEqual(self.suite(permitir_lanzar=False)[1], Completo('verde', costo_s=8))
        self.assertEqual(self.invocaciones(), procesos)
        self.assertEqual(len(self.lanzamientos()), 2)

    def test_boat_mapa_por_modo_y_repeticion_filtrada_en_el_host_de_su_modo(self):
        from adaptadores import seleccionar
        config = {'adaptador': 'jale', 'lanzador': str(self.lanzador),
                  'host': {'contract': 'boat', 'e2e': 'desktop'}}
        self.adaptador = seleccionar(config, None)
        origen = self.origen()
        self.assertCountEqual([(a[0], a[2]) for a in self.lanzamientos()],
                             [('boat', 'contract'), ('desktop', 'e2e')])
        self.plan([{}])
        with patch.dict(os.environ, {'REMOTE_RUN_CLAVE': 'clave-heredada',
                                     'SEN_E2E_FRAGMENTO': '1/2', 'TURBO_FORCE': 'false'}):
            self.assertIsInstance(self.repetir(origen), Repeticion)
        self.assertEqual([(a[0], a[4:]) for a in self.lanzamientos()[2:]],
                         [('boat', [UNIDAD]), ('boat', [UNIDAD])])
        repeticiones = self.invocaciones()[2:]
        for i in repeticiones:
            variables = dict(i['boat'])
            self.assertRegex(variables.pop('SEN_RUN_ID'), r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
            self.assertEqual(variables, {'REMOTE_RUN_CLAVE': None, 'SEN_E2E_FRAGMENTO': None,
                'SEN_RUN_OWNER': 'pao-tren', 'SEN_BOAT_PRIORIDAD': 'tren',
                'SEN_BOAT_ESPERAR': '180', 'TURBO_FORCE': 'true'})
        self.assertNotEqual(*[i['boat']['SEN_RUN_ID'] for i in repeticiones])

    def test_repeticion_filtrada_fuera_de_boat_conserva_su_host(self):
        self.adaptador = self.Jale(self.lanzador, host={'contract': 'desktop', 'e2e': 'boat'})
        origen = self.origen()
        self.plan([{}])
        self.assertIsInstance(self.repetir(origen), Repeticion)
        self.assertEqual([(a[0], a[4:]) for a in self.lanzamientos()[2:]],
                         [('desktop', [UNIDAD]), ('desktop', [UNIDAD])])

    def test_boat_repeticion_pendiente_reengancha_la_misma_corrida(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        origen = self.origen()
        self.plan([{}, {'pendiente': 'intento desconocido'}, {}])
        resultado = self.repetir(origen)
        self.assertIsInstance(resultado, Incompleto)
        self.assertEqual(resultado.codigo, 75)
        self.assertIsInstance(self.repetir(origen, intento=1, reanudar=resultado.reanudar),
                              Repeticion)
        primera, segunda, tercera = self.invocaciones()[1:]
        self.assertEqual([Path(i['log']).parent.name for i in (primera, segunda, tercera)],
                         ['0', '1', '1'])
        self.assertEqual(Path(primera['log']).parent.parent, Path(segunda['log']).parent.parent)
        self.assertEqual(segunda['boat']['SEN_RUN_ID'], primera['boat']['SEN_RUN_ID'])
        self.assertNotEqual(tercera['boat']['SEN_RUN_ID'], primera['boat']['SEN_RUN_ID'])
        self.assertEqual([a[4:] for a in self.lanzamientos()[1:]], [[UNIDAD], [UNIDAD]])

    def test_boat_repeticion_sin_lugar_lee_el_recibo_del_desk(self):
        self.adaptador = self.Jale(self.lanzador, host='boat', modos=('contract',))
        origen = self.origen()
        self.plan([{'fallback': True}])
        self.assertIsInstance(self.repetir(origen), Repeticion)
        repeticiones = self.invocaciones()[1:]
        self.assertEqual([i['host'] for i in repeticiones], ['boat', 'boat'])
        for i in repeticiones:
            self.assertIn('HOST=desktop fallback=sin-lugar', Path(i['log']).read_text())

    def test_repeticion_lanzada_en_auto_se_recupera_con_el_modo_ya_en_boat(self):
        origen = self.origen()
        self.plan([{}])
        self.assertIsInstance(self.repetir(origen), Repeticion)
        lanzadas = self.lanzamientos()
        self.assertEqual([a[0] for a in lanzadas[2:]], ['auto', 'auto'])
        self.adaptador = self.Jale(self.lanzador, host='boat')
        self.assertIsInstance(self.repetir(origen, permitir_lanzar=False), Repeticion)
        self.assertEqual(self.lanzamientos(), lanzadas)

    def test_boat_config_valida_host_y_espera(self):
        from adaptadores import seleccionar
        config = {'adaptador': 'jale', 'lanzador': str(self.lanzador)}
        for host in ('boat', {'contract': 'auto', 'e2e': 'boat'}):
            self.assertEqual(seleccionar(dict(config, host=host), None).host, host)
            self.assertEqual(self.Jale(self.lanzador, host=host).host, host)
        for host in ('pruebas', ['boat'], {'contract': 'boat'}, {'e2e': 'boat'},
                     {'contract': 'boat', 'e2e': 'pruebas'}, {'contract': 'boat', 'e2e': 1},
                     {'contract': 'boat', 'e2e': 'boat', 'unit': 'auto'},
                     {'contract': 'boat', 'e2e': []}, [], None, {}):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, '^host-invalido$'):
                    seleccionar(dict(config, host=host), None)
                with self.assertRaisesRegex(ValueError, '^host-invalido$'):
                    self.Jale(self.lanzador, host=host)
        for host in ({'e2e': 'boat'}, {'contract': 'auto', 'e2e': 'boat'}):
            self.assertEqual(seleccionar(dict(config, host=host, modos=['e2e']), None).host, host)
            self.assertEqual(self.Jale(self.lanzador, host=host, modos=('e2e',)).host, host)
        for espera in (-1, 7201, True, '42'):
            with self.subTest(espera=espera), self.assertRaisesRegex(ValueError, '^boat-esperar-invalido$'):
                seleccionar(dict(config, host='boat', boat_esperar_s=espera), None)

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
        self.assertEqual(esperar_resultado(jale.suite, CANDIDATO, 'config')[1].costo_s, 4)
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
            esperar_resultado(ejecutar, efecto, inicial, obs, object(), adaptador=self.adaptador)
        self.assertEqual(contexto.exception.resultado.codigo, 'sha-no-corresponde')
