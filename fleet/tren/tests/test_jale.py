"""U9: contrato por fixtures; ejecutar sólo en el host de pruebas."""
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import shlex
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

UNIDAD = 'apps/api/src/a.contract.test.ts'
OTRA = 'apps/api/src/b.contract.test.ts'
FIRMA = 'sha256:' + 'a' * 64
FALLO = Fallo(UNIDAD, FIRMA, 'solo-candidato', 'contract', 'contract', 'jale', 4)

# Escribe exclusivamente fixtures del contrato; no depende del productor real.
PRODUCTOR = r'''
import base64
import json
from pathlib import Path
import sys
import time
raiz = Path(sys.argv[1])
host, log, modo, sha, *filtros = sys.argv[2:]
contador = raiz / 'lanzamientos.jsonl'
previos = contador.read_text().splitlines() if contador.exists() else []
plan = json.loads((raiz / 'plan.json').read_text())
f = plan[min(len(previos), len(plan) - 1)]
with contador.open('a') as archivo:
    archivo.write(json.dumps([host, log, modo, sha, *filtros]) + '\n')
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
Path(log).write_text('' if f.get('sin_linea') else linea)
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
        self.invalido({'codigo': 64, 'sin_linea': True}, 'jale-paro-64')
        with self.assertRaises(ResultadoInvalido):
            self.suite()
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

    def test_completo_incoherente(self):
        self.invalido({'estados': ['falla', 'no-corrida'], 'cambiar': {'completo': True}},
                      'veredicto-incoherente')

    def test_unidad_duplicada(self):
        self.invalido({'duplicar_unidad': True}, 'unidades-desordenadas-o-duplicadas')

    def test_unidades_desordenadas(self):
        self.invalido({'estados': ['falla', 'pasa'], 'invertir': True},
                      'unidades-desordenadas-o-duplicadas')

    def test_sin_linea_con_salida_cero(self):
        self.invalido({'sin_linea': True}, 'salida-sin-recibo')

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
        (registro / 'solicitud.json').write_text(json.dumps(['bash', str(self.lanzador),
            'auto', str(registro / 'corrida.log'), 'contract', C]))
        (registro / 'iniciado.json').write_text('true')
        with self.assertRaises(ResultadoInvalido) as contexto:
            self.suite()
        self.assertEqual(contexto.exception.resultado.codigo, 'corrida-interrumpida')
        self.assertEqual(self.lanzamientos(), [])

    def test_seleccion_configurable_y_hasito_legacy(self):
        from adaptadores import seleccionar
        git = object()
        legacy = seleccionar({'lanzador_hasito': str(self.lanzador)}, git)
        self.assertIsInstance(legacy, Hasito)
        self.assertIs(legacy.git, git)
        self.assertEqual(legacy.lanzador, str(self.lanzador))
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
