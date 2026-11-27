"""U3: prelanzamiento especulativo con un productor de recibos sintético."""
from dataclasses import replace
from itertools import count
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_activo as activo
import test_jale as jale
from test_hasito import CANDIDATO
from test_tren import Aislado, estado, observacion
from adaptadores import seleccionar
from adaptadores.ejecucion_jale import guardar
from adaptadores.hasito import Hasito, ResultadoInvalido
from adaptadores.jale import EN_CURSO, Jale, PLAZO_PRELANZAMIENTO_S, corrida_en_curso
from atribucion import planificar
from dominio import (Armado, Certificado, Corriendo, Diagnosticando, Efecto, EscritorExterno,
                     EsperandoCI, Pausa, Promoviendo, ResultadoCI, Terminal, Tren, clave,
                     efecto_promover, efecto_reanudar_suite)
from efectos import EfectoFallido, ejecutar
from git_ops import ErrorGit, Publicado, RamaOcupada
from ledger import Ledger
from operador import FalloSnapshot, lanzamiento_activo

REPO = activo.REPO


LANZADOR_CANCELABLE = r'''
import fcntl
import json
from pathlib import Path
import signal
import sys
import time

raiz = Path(sys.argv[1])
registro = Path(sys.argv[3]).parent
limite = time.monotonic() + 15

def esperar(ruta):
    while not ruta.exists() and time.monotonic() < limite:
        time.sleep(.01)

def cancelar(signum, frame):
    (registro / 'term').touch()
    if (raiz / 'retener-cancelacion').exists():
        esperar(raiz / 'salir')
    else:
        time.sleep(.2)
    codigo = raiz / 'codigo-cancelacion'
    sys.exit(int(codigo.read_text()) if codigo.exists() else 75)

signal.signal(signal.SIGTERM, cancelar)
with (registro / 'invocaciones').open('a') as archivo:
    archivo.write('1\n')
predecesor = raiz / 'predecesor.json'
if predecesor.exists():
    activos = []
    for ruta in json.loads(predecesor.read_text()):
        with Path(ruta, 'candado').open('rb') as candado:
            try:
                fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                activos.append(ruta)
    (registro / 'predecesoras-activas.json').write_text(json.dumps(activos))
(registro / 'listo').touch()
esperar(raiz / 'salir')
'''


class Productor:
    plan = jale.JaleTests.plan
    lanzamientos = jale.JaleTests.lanzamientos
    invocaciones = jale.JaleTests.invocaciones

    def preparar_productor(self):
        productor = self.raiz / 'productor.py'
        productor.write_text(jale.PRODUCTOR)
        self.lanzador = self.raiz / 'run-remote.sh'
        self.lanzador.write_text('#!/bin/bash\nexec ' + shlex.join(
            [sys.executable, str(productor), str(self.raiz)]) + ' "$@"\n')
        self.plan([{}])
        self.addCleanup((self.raiz / 'seguir').touch)

    def esperar_archivo(self, ruta):
        limite = time.monotonic() + 5
        while not ruta.exists() and time.monotonic() < limite:
            time.sleep(.01)
        self.assertTrue(ruta.exists(), str(ruta))


class PrelanzarTests(Productor, Aislado):
    def setUp(self):
        super().setUp()
        self.preparar_productor()
        self.adaptador = Jale(self.lanzador, modos=('contract',), prelanzar=True)
        self.registro = self.adaptador.estado / clave('suite', 'lote') / 'contract' / '0'

    def prelanzar(self):
        self.adaptador.prelanzar(CANDIDATO, 'lote')

    def suite(self, permitir):
        return jale.esperar_resultado(self.adaptador.suite, CANDIDATO, 'lote', permitir_lanzar=permitir)

    def test_pre_lanza_una_vez_y_la_suite_recupera_el_resultado_confirmado(self):
        self.plan([{'esperar': True}])
        self.prelanzar()
        self.assertTrue(lanzamiento_activo(self.registro / 'candado'))
        self.esperar_archivo(self.raiz / 'listo')
        self.assertFalse((self.registro / 'codigo.json').exists())
        self.prelanzar()
        (self.raiz / 'seguir').touch()
        resultado = self.suite(True)
        self.assertEqual(resultado[1].veredicto, 'verde')
        self.assertEqual(self.suite(False), resultado)
        self.prelanzar()
        self.assertEqual(len(self.lanzamientos()), 1)
        self.assertEqual(len(self.invocaciones()), 1)

    def test_terminada_sin_confirmar_no_se_vuelve_a_lanzar(self):
        self.prelanzar()
        self.esperar_archivo(self.registro / 'codigo.json')
        self.prelanzar()
        self.assertEqual(len(self.invocaciones()), 1)

    def test_suite_sin_permiso_sondea_el_modo_especulativo_vivo(self):
        self.plan([{'esperar': True}])
        self.prelanzar()
        self.esperar_archivo(self.raiz / 'listo')
        adaptador = Jale(self.lanzador)
        self.assertIs(adaptador.suite(CANDIDATO, 'lote', permitir_lanzar=False), EN_CURSO)
        self.assertFalse((self.registro / 'confirmada.json').exists())
        self.assertFalse((self.registro.parents[1] / 'e2e' / '0' / 'solicitud.json').exists())
        (self.raiz / 'seguir').touch()
        self.esperar_archivo(self.registro / 'codigo.json')
        self.assertIsNone(adaptador.suite(CANDIDATO, 'lote', permitir_lanzar=False))

    def test_interruptor_apagado_por_omision_y_validacion_booleana(self):
        config = {'adaptador': 'jale', 'lanzador': str(self.lanzador)}
        for campo in ({}, {'prelanzar': False}):
            seleccionar(dict(config, **campo), None).prelanzar(CANDIDATO, 'lote')
        self.assertFalse(self.registro.parent.parent.exists())
        self.assertEqual(self.lanzamientos(), [])
        for valor in ('true', 1, None):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, 'prelanzar-invalido'):
                seleccionar(dict(config, prelanzar=valor), None)

    def test_sondea_hasta_candado_salida_o_tope_y_lanza_aislado(self):
        for tren, sondeos, traba, dormidas in (
                ('candado', None, (False, True), 1),
                ('salida', (None, 0), (False,), 1),
                ('tope', None, False, PLAZO_PRELANZAMIENTO_S * 100)):
            with self.subTest(tren=tren):
                proceso = Mock()
                proceso.poll.return_value = None
                proceso.poll.side_effect = sondeos
                with patch('adaptadores.jale.subprocess.Popen', return_value=proceso) as popen, \
                     patch('adaptadores.jale.lanzamiento_activo',
                           side_effect=traba if isinstance(traba, tuple) else None,
                           return_value=False), \
                     patch('adaptadores.jale.time.sleep') as dormir, \
                     patch('adaptadores.jale.time.monotonic',
                           side_effect=lambda: dormir.call_count / 100):
                    self.adaptador.prelanzar(CANDIDATO, tren)
                self.assertEqual(dormir.call_count, dormidas)
                self.assertEqual(popen.call_args.args[0][:2], [sys.executable, '-I'])
                proceso.wait.assert_not_called()

    def test_recuperacion_sin_verde_no_espera_ni_lee_el_registro_especulativo(self):
        self.prelanzar()
        self.esperar_archivo(self.registro / 'codigo.json')
        inicial, tren = estado_ci()
        efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
        obs = observacion(ci_candidato='rojo', tren_id=tren.id)
        with patch('adaptadores.jale.leer_salida') as leer, \
             patch('adaptadores.jale.subprocess.Popen') as popen:
            self.assertIsNone(self.suite(False))
            ejecucion = ejecutar(efecto, inicial, obs, object(), self.adaptador)
            self.assertFalse(ejecucion.completo)
            self.assertEqual(ejecucion.observacion.ci_candidato, 'rojo')
            self.assertIsNone(ejecucion.observacion.resultado_suite)
            leer.assert_not_called()
            popen.assert_not_called()
        verde = ejecutar(efecto, inicial, replace(obs, ci_candidato='verde'), object(),
                         self.adaptador)
        self.assertTrue(verde.completo)
        self.assertEqual(self.suite(False)[1], verde.observacion.resultado_suite)
        self.assertEqual(len(self.invocaciones()), 1)


def estado_ci():
    inicial = estado()
    tren = Tren('lote', (inicial.entradas[0].entrega.recibo,), EsperandoCI(CANDIDATO))
    return replace(inicial, trenes=(tren,)), tren


class SolicitudTests(Aislado):
    def setUp(self):
        super().setUp()
        self.estado, tren = estado_ci()
        self.efecto = Efecto(clave('ci', tren.id), 'solicitar-ci', (tren.id,))
        self.obs = observacion(ci_candidato='verde')
        self.git = Mock()
        self.git.publicar.return_value = Publicado('refs/heads/tren/lote', CANDIDATO.C)

    def ejecutar(self, adaptador=None):
        return ejecutar(self.efecto, self.estado, self.obs, self.git, adaptador)

    def test_aviso_tipado_y_sin_detalles_privados(self):
        base = self.ejecutar()
        self.assertEqual(self.ejecutar(Hasito(self.raiz / 'lanzador', self.git)), base)
        self.assertTrue(base.completo)
        self.assertEqual(base.observacion.ci_candidato, 'pendiente')
        for error, motivo in (
                (ResultadoInvalido('recibo-no-corresponde'), 'recibo-no-corresponde'),
                (FalloSnapshot('snapshot-no-verificable'), 'snapshot-no-verificable'),
                (OSError('/privado/lanzador'), 'OSError'),
                (subprocess.CalledProcessError(2, ['/privado/lanzador'], stderr='privado'),
                 'CalledProcessError')):
            with self.subTest(error=type(error).__name__):
                adaptador = Mock()
                adaptador.prelanzar.side_effect = error
                ejecucion = self.ejecutar(adaptador)
                adaptador.prelanzar.assert_called_once_with(CANDIDATO, 'lote')
                self.assertEqual(replace(ejecucion, aviso=None), base)
                self.assertEqual(ejecucion.aviso, dict(tren='lote', motivo=motivo,
                                                      clase=type(error).__name__))

    def test_publicacion_ocupada_o_fallida_no_prelanza(self):
        adaptador = Mock()
        self.git.publicar.return_value = RamaOcupada('refs/heads/tren/lote', CANDIDATO.C, 'a' * 40)
        self.assertEqual(self.ejecutar(adaptador), self.ejecutar())
        self.git.publicar.return_value = ErrorGit('publicacion-rechazada')
        with self.assertRaises(EfectoFallido):
            self.ejecutar(adaptador)
        adaptador.prelanzar.assert_not_called()


class TickActivoTests(Productor, unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    lineas = activo.ActivoTests.lineas

    def setUp(self):
        activo.ActivoTests.setUp(self)
        self.preparar_productor()
        self.privada = self.raiz / 'config.json'
        self.configurar(True)
        self.ci = 'pendiente'
        self.reloj = count(100)
        original = self.contexto.observador.ci
        heads = {head for _, head in self.miembros}
        self.contexto.observador.ci = lambda repo, commit, rama=None: (
            original(repo, commit, rama) if commit in heads else
            ResultadoCI(self.ci, ('check',) if self.ci == 'rojo' else ()))

    def configurar(self, prelanzar):
        config = dict(self.contexto.config, adaptador='jale', prelanzar=prelanzar,
                      lanzador=str(self.lanzador))
        self.privada.write_text(json.dumps({'repos': {REPO: config}}))

    def cli(self, *argv, frontera=None):
        args = activo.cli.parser().parse_args([*argv, '--repo', REPO])
        with patch('git_ops.GitTren', return_value=self.contexto.git), \
             patch.object(activo.cli, 'GitHubGit', return_value=self.contexto.observador):
            codigo, salida = activo.cli.ejecutar(args, ahora=next(self.reloj), frontera=frontera,
                                                 escritor=self.contexto.escritor)
        self.assertEqual(codigo, 0, salida)
        return salida

    def tick(self, frontera=None):
        return self.cli('tick', '--config', str(self.privada), frontera=frontera)

    def hasta(self, fase, indice=0):
        for _ in range(8):
            resultado = self.tick()
            trenes = self.estado().trenes
            if trenes and isinstance(trenes[indice].fase, fase):
                return trenes[indice], resultado
        self.fail('fase no alcanzada: ' + fase.__name__)

    def eventos(self, tipo):
        with self.ledger() as ledger:
            filas = ledger.db.execute('SELECT payload FROM events WHERE repo=? AND tipo=?'
                                      ' ORDER BY seq DESC', (REPO, tipo))
            return [json.loads(fila[0]) for fila in filas]

    def test_tick_publica_y_prelanza_con_ci_pendiente_y_recoge_en_verde(self):
        self.plan([{'barrera': True, 'esperar': True}])
        _, resultado = self.hasta(EsperandoCI)
        self.assertTrue(any(':refs/heads/tren/' in p for p in self.lineas('pushes.log')))
        self.assertTrue(any(f['efecto']['tipo'] == 'solicitar-ci' and f['estado'] == 'resultado'
                            for f in resultado['outbox']))
        for modo in ('contract', 'e2e'):
            self.esperar_archivo(self.raiz / ('cruzado-' + modo))
        self.assertCountEqual([fila[2] for fila in self.lanzamientos()], ['contract', 'e2e'])
        self.ci = 'verde'
        (self.raiz / 'seguir').touch()
        for modo in ('contract', 'e2e'):
            registro = Jale(self.lanzador).estado / clave('suite', self.estado().trenes[0].id)
            self.esperar_archivo(registro / modo / '0' / 'codigo.json')
        resultado = self.tick()
        self.assertTrue(any(f['efecto']['tipo'] == 'iniciar-suite' and f['estado'] == 'resultado'
                            for f in resultado['outbox']))
        self.hasta(Certificado)
        self.assertCountEqual([fila[2] for fila in self.lanzamientos()], ['contract', 'e2e'])
        self.assertEqual(len(self.invocaciones()), 2)
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), [])

    def test_apagar_en_config_surte_efecto_en_el_siguiente_tick(self):
        self.hasta(Armado)
        self.configurar(False)
        self.hasta(EsperandoCI)
        self.assertEqual(self.invocaciones(), [])
        self.ci = 'rojo'
        self.hasta(Terminal)
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), [])

    def test_pre_lanzamiento_fallido_visible_y_ci_con_resultado(self):
        with patch.object(Jale, 'prelanzar', side_effect=OSError('/privado')):
            tren, resultado = self.hasta(EsperandoCI)
        self.assertEqual(self.eventos('prelanzamiento-fallido'),
                         [dict(tren=tren.id, motivo='OSError', clase='OSError')])
        self.assertTrue(any(f['efecto']['tipo'] == 'solicitar-ci' and f['estado'] == 'resultado'
                            for f in resultado['outbox']))

    def test_reintento_de_solicitar_ci_con_ci_ya_rojo_no_prelanza(self):
        self.hasta(Armado)
        with patch.object(Jale, 'prelanzar', side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            self.tick()
        self.ci = 'rojo'
        self.hasta(Terminal)
        self.assertEqual(sum(':refs/heads/tren/' in p for p in self.lineas('pushes.log')), 1)
        self.assertEqual(self.invocaciones(), [])
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), [])

    def test_reanudar_el_ci_prelanza_aunque_la_solicitud_anterior_lea_rojo(self):
        self.configurar(False)
        tren, _ = self.hasta(EsperandoCI)
        self.reloj = count(4000)
        self.tick()
        self.assertEqual(self.estado().pausa.detalle, 'ci-sin-resultado')
        self.configurar(True)
        self.ci = 'rojo'
        anterior = self.contexto.observador.ci
        self.contexto.observador.ci = lambda repo, commit, rama=None: (
            ResultadoCI('pendiente', ()) if rama == 'tren/' + tren.id + '-ci-1'
            else anterior(repo, commit, rama))
        self.cli('juicio', '--tren', tren.id, '--decision', 'reanudar', '--motivo', 'prueba')
        self.tick()
        [vivo] = [t for t in self.estado().trenes if t.id == tren.id]
        self.assertEqual((type(vivo.fase), vivo.fase.solicitud), (EsperandoCI, 1))
        for modo in ('contract', 'e2e'):
            self.esperar_archivo(Jale(self.lanzador).estado / clave('suite', tren.id) /
                                 modo / '0' / 'codigo.json')
        self.assertCountEqual([fila[2] for fila in self.lanzamientos()], ['contract', 'e2e'])

    def assert_sin_leer_por_modo(self, tren):
        esperado = []
        for modo in ('contract', 'e2e'):
            registro = Jale(self.lanzador).estado / clave('suite', tren.id) / modo / '0'
            self.esperar_archivo(registro / 'codigo.json')
            iniciado = json.loads((registro / 'iniciado.json').read_text())
            esperado.append(dict(tren=tren.id, modo=modo, clave=iniciado['clave'], terminada=True,
                                 cancelacion_pedida=False, cancelacion_confirmada=False))
        self.hasta(Terminal)
        self.assertCountEqual(self.eventos('suite-prelanzada-sin-leer'), esperado)
        self.tick()
        with self.ledger() as ledger:
            filas = ledger.db.execute("SELECT payload FROM events WHERE repo=? AND tipo=?",
                                      (REPO, 'suite-prelanzada-sin-leer'))
            self.assertCountEqual([json.loads(fila[0]) for fila in filas], esperado)

    def test_ci_rojo_rearma_y_no_acredita_suites_prelanzadas_sin_verde(self):
        tren, _ = self.hasta(EsperandoCI)
        self.ci = 'verde'

        def detener(nombre, efecto):
            if nombre == 'antes-ejecutar' and efecto.tipo == 'iniciar-suite':
                raise InterruptedError()

        with self.assertRaises(InterruptedError):
            self.tick(detener)
        self.ci = 'rojo'
        self.assert_sin_leer_por_modo(tren)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.ci = 'pendiente'
        nuevo, _ = self.hasta(EsperandoCI, -1)
        self.assertNotEqual(nuevo.id, tren.id)
        self.assertEqual(nuevo.fase.candidato.C, tren.fase.candidato.C)
        self.assertEqual(nuevo.fase.solicitud, 1)
        raiz = Jale(self.lanzador).estado
        for modo in ('contract', 'e2e'):
            self.esperar_archivo(raiz / clave('suite', nuevo.id) / modo / '0' / 'codigo.json')
        resultado = self.tick()
        self.assertEqual(self.estado().trenes[-1], nuevo)
        self.assertFalse(any(f['efecto']['tipo'] == 'iniciar-suite' for f in resultado['outbox']))
        for id_tren in (tren.id, nuevo.id):
            for modo in ('contract', 'e2e'):
                self.assertFalse((raiz / clave('suite', id_tren) / modo / '0' / 'confirmada.json').exists())
        self.ci = 'verde'
        certificado, _ = self.hasta(Certificado, -1)
        self.assertEqual(certificado.fase.ci, 'github:' + nuevo.fase.candidato.C +
                         ':tren/' + nuevo.id + '-ci-1')
        self.assertEqual(Path(certificado.fase.suite).parent, raiz / clave('suite', nuevo.id))

    def abortar(self, tren):
        self.cli('juicio', '--tren', tren.id, '--decision', 'abortar', '--motivo', 'prueba')

    def instalar_lanzador_cancelable(self):
        productor = self.raiz / 'cancelable.py'
        productor.write_text(LANZADOR_CANCELABLE)
        self.lanzador.write_text('#!/bin/bash\nexec ' + shlex.join(
            [sys.executable, str(productor), str(self.raiz)]) + ' "$@"\n')
        self.addCleanup(self.liberar_lanzadores)

    def liberar_lanzadores(self):
        (self.raiz / 'salir').touch()
        for archivo in Jale(self.lanzador).estado.rglob('invocaciones'):
            self.esperar_candado_libre(archivo.parent)

    def esperar_candado_libre(self, registro):
        candado = registro / 'candado'
        limite = time.monotonic() + 5
        while lanzamiento_activo(candado) and time.monotonic() < limite:
            time.sleep(.01)
        self.assertFalse(lanzamiento_activo(candado))

    def registros_de_suite(self, tren, intento=0):
        raiz = Jale(self.lanzador).estado / clave('suite', tren.id)
        return [raiz / modo / str(intento) for modo in ('contract', 'e2e')]

    def registrar_fase(self, tren, fase, pausa=None, efectos=()):
        actual = self.estado()
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(actual, trenes=(replace(tren, fase=fase),), pausa=pausa),
                                efectos, next(self.reloj))

    def lote_con_prelanzadas(self):
        self.instalar_lanzador_cancelable()
        tren, _ = self.hasta(EsperandoCI)
        registros = self.registros_de_suite(tren)
        for registro in registros:
            self.esperar_archivo(registro / 'listo')
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
        return tren, registros

    def lote_con_confirmadas(self, intento=0):
        self.instalar_lanzador_cancelable()
        self.configurar(False)
        tren, _ = self.hasta(EsperandoCI)
        if intento:
            fase = Corriendo(tren.fase.candidato, 'suite.json', reanudaciones=intento)
            efecto = efecto_reanudar_suite(tren, fase)
            self.registrar_fase(tren, fase, efectos=(efecto,))
        else:
            efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
        self.ci = 'verde'
        resultado = self.tick()
        [fila] = [f for f in resultado['outbox'] if f['efecto']['tipo'] == efecto.tipo]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        registros = self.registros_de_suite(tren, intento)
        for registro in registros:
            self.esperar_archivo(registro / 'listo')
            self.assertFalse((registro / 'prelanzada.json').exists())
            self.assertTrue(corrida_en_curso(registro))
        self.configurar(True)
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        return tren, registros, efecto.clave

    def assert_canceladas(self, registros, codigo=75):
        for registro in registros:
            self.assertTrue((registro / 'cancelar.json').exists())
            self.assertTrue((registro / 'term').exists())
            self.assertEqual(json.loads((registro / 'codigo.json').read_text()), codigo)
            self.assertFalse(lanzamiento_activo(registro / 'candado'))
            self.assertEqual((registro / 'invocaciones').read_text().splitlines(), ['1'])

    def assert_avisos_cancelacion(self, tren, cantidad, confirmada,
                                  evento='suite-prelanzada-sin-leer'):
        avisos = [a for a in self.eventos(evento) if a['tren'] == tren.id]
        self.assertEqual(len(avisos), cantidad)
        for aviso in avisos:
            self.assertTrue(aviso['cancelacion_pedida'])
            self.assertEqual(aviso['cancelacion_confirmada'], confirmada)

    def assert_outbox_cancelado(self, clave_efecto):
        with self.ledger() as ledger:
            fila = ledger.db.execute('SELECT o.estado, r.payload FROM outbox o '
                                     'LEFT JOIN effect_results r ON o.clave=r.clave '
                                     'WHERE o.clave=?', (clave_efecto,)).fetchone()
            self.assertEqual(tuple(fila), ('cancelado', None))

    def esperar_sucesor_ci(self, tren):
        self.ci = 'pendiente'
        for _ in range(8):
            self.tick()
            sucesor = self.estado().trenes[-1]
            if sucesor.id != tren.id and isinstance(sucesor.fase, EsperandoCI):
                return sucesor
        self.fail('el sucesor no llegó a EsperandoCI')

    def assert_sucesor_sin_candados(self, sucesor):
        for registro in self.registros_de_suite(sucesor):
            self.esperar_archivo(registro / 'listo')
            self.assertEqual(json.loads((registro / 'predecesoras-activas.json').read_text()), [])

    def test_rearme_cancela_y_suelta_antes_de_prelanzar_sucesor(self):
        tren, registros = self.lote_con_prelanzadas()
        self.ci = 'rojo'
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assert_canceladas(registros)
        self.assert_avisos_cancelacion(tren, 2, True)
        adaptador = Jale(self.lanzador, prelanzar=True)
        adaptador.prelanzar(tren.fase.candidato, tren.id)
        with self.assertRaisesRegex(ResultadoInvalido, 'corrida-interrumpida'):
            adaptador.suite(tren.fase.candidato, tren.id)
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        self.assert_sucesor_sin_candados(self.esperar_sucesor_ci(tren))

    def test_rearme_aplaza_con_candado_y_prelanza_en_el_tick_siguiente_a_la_salida(self):
        tren, registros = self.lote_con_prelanzadas()
        (self.raiz / 'retener-cancelacion').touch()
        self.ci = 'rojo'
        with patch('adaptadores.jale.PLAZO_CANCELACION_S', .5):
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assert_avisos_cancelacion(tren, 2, False)
        for registro in registros:
            self.esperar_archivo(registro / 'term')
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
        adaptador = Jale(self.lanzador)
        self.assertFalse(adaptador.suite_con_coordinador(tren.id))
        with self.assertRaisesRegex(ResultadoInvalido, 'corrida-interrumpida'):
            adaptador.suite(tren.fase.candidato, tren.id)
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        sucesor = self.esperar_sucesor_ci(tren)
        self.assertTrue(all(not r.exists() for r in self.registros_de_suite(sucesor)))
        self.assertEqual(sum(':refs/heads/tren/' in p for p in self.lineas('pushes.log')), 2)
        (self.raiz / 'salir').touch()
        for registro in registros:
            self.esperar_candado_libre(registro)
        self.assert_canceladas(registros)
        self.assertFalse(adaptador.suite_con_coordinador(tren.id))
        self.tick()
        self.assert_sucesor_sin_candados(sucesor)
        self.assertEqual(sum(':refs/heads/tren/' in p for p in self.lineas('pushes.log')), 2)

    def abortar_con_head_cambiado(self, tren):
        (self.raiz / 'pr-cerrado-1').touch()
        self.tick()
        self.assertEqual(self.estado().pausa.motivo, 'head-cambiado-en-tren')
        self.abortar(tren)

    def comprobar_cierre_con_confirmadas(self, tren, registros, efecto, estado):
        with patch('adaptadores.jale.PLAZO_CANCELACION_S', 3), \
             patch('adaptadores.jale.leer_salida') as leer:
            self.tick()
            self.assertEqual(self.estado().trenes[0].fase, Terminal(estado))
            self.assert_canceladas(registros)
            self.assert_sucesor_sin_candados(self.esperar_sucesor_ci(tren))
            leer.assert_not_called()
        self.assertIsNone(self.estado().pausa)
        self.assert_avisos_cancelacion(tren, 2, True, 'suite-sin-leer')
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), [])
        self.assert_outbox_cancelado(efecto)

    def test_aborto_cancela_confirmadas_antes_de_prelanzar_sucesor(self):
        tren, registros, efecto = self.lote_con_confirmadas()
        self.abortar_con_head_cambiado(tren)
        self.comprobar_cierre_con_confirmadas(tren, registros, efecto, 'ABORTADO')

    def test_rearme_cancela_confirmadas_antes_de_prelanzar_sucesor(self):
        tren, registros, efecto = self.lote_con_confirmadas()
        self.ci = 'rojo'
        self.comprobar_cierre_con_confirmadas(tren, registros, efecto, 'REARMAR')

    def test_aborto_aplaza_por_candados_de_suite_reanudada(self):
        tren, registros, efecto = self.lote_con_confirmadas(intento=1)
        (self.raiz / 'retener-cancelacion').touch()
        self.abortar_con_head_cambiado(tren)
        with patch('adaptadores.jale.PLAZO_CANCELACION_S', .5):
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assert_avisos_cancelacion(tren, 2, False, 'suite-sin-leer')
        self.assert_outbox_cancelado(efecto)
        for registro in registros:
            self.esperar_archivo(registro / 'term')
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
        sucesor = self.esperar_sucesor_ci(tren)
        aplazado = Jale(self.lanzador).archivo_prelanzamiento_aplazado(sucesor.id)
        self.assertTrue(aplazado.exists())
        self.assertFalse(any(r.exists() for r in self.registros_de_suite(sucesor)))
        (self.raiz / 'salir').touch()
        for registro in registros:
            self.esperar_candado_libre(registro)
        self.assert_canceladas(registros)
        self.tick()
        self.assert_sucesor_sin_candados(sucesor)
        self.assertFalse(aplazado.exists())

    def test_reintento_del_aborto_tras_fallo_sqlite_conserva_los_avisos(self):
        tren, registros, efecto = self.lote_con_confirmadas()
        self.abortar_con_head_cambiado(tren)
        guardar_evento = Ledger.evento

        def fallar_al_guardar_paso(ledger, repo, tipo, ahora, payload):
            if tipo == 'operativo':
                raise sqlite3.OperationalError('fallo al guardar el cierre')
            guardar_evento(ledger, repo, tipo, ahora, payload)

        with patch.object(Ledger, 'evento', fallar_al_guardar_paso), \
                patch('adaptadores.jale.PLAZO_CANCELACION_S', 3):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'guardar el cierre'):
                self.tick()
        self.assert_canceladas(registros)
        self.assertNotIsInstance(self.estado().trenes[0].fase, Terminal)
        self.assertEqual(self.eventos('suite-sin-leer'), [])
        with self.ledger() as ledger:
            self.assertEqual(ledger.siguiente_decision(REPO).tren, tren.id)

        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assert_avisos_cancelacion(tren, 2, True, 'suite-sin-leer')
        avisos = self.eventos('suite-sin-leer')
        self.assertCountEqual([a['modo'] for a in avisos], ['contract', 'e2e'])
        for aviso in avisos:
            self.assertFalse(aviso['terminada'])
            self.assertNotIn('error_cancelacion', aviso)
        self.assert_outbox_cancelado(efecto)
        self.assert_sucesor_sin_candados(self.esperar_sucesor_ci(tren))
        self.assertEqual(self.eventos('suite-sin-leer'), avisos)

    def test_reintento_del_aborto_tras_las_marcas_reconstruye_la_barrera(self):
        tren, registros, _ = self.lote_con_confirmadas()
        (self.raiz / 'retener-cancelacion').touch()
        self.abortar_con_head_cambiado(tren)
        adaptador = Jale(self.lanzador)
        adaptador.operador = SimpleNamespace(repo=REPO)

        def guardar_y_cortar(ruta, valor):
            guardar(ruta, valor)
            if ruta == registros[-1] / 'cancelar.json':
                raise RuntimeError('corte tras la última marca')

        with patch('adaptadores.jale.guardar', side_effect=guardar_y_cortar):
            with self.assertRaisesRegex(RuntimeError, 'corte tras la última marca'):
                self.tick()
        self.assertNotIsInstance(self.estado().trenes[0].fase, Terminal)
        self.assertFalse(adaptador.archivo_cancelaciones().exists())
        for registro in registros:
            self.assertTrue((registro / 'cancelar.json').exists())
            self.esperar_archivo(registro / 'term')

        with patch('adaptadores.jale.PLAZO_CANCELACION_S', .5):
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        sucesor = self.esperar_sucesor_ci(tren)
        aplazado = adaptador.archivo_prelanzamiento_aplazado(sucesor.id)
        self.assertEqual((sum(lanzamiento_activo(r / 'candado') for r in registros),
                          sum(r.exists() for r in self.registros_de_suite(sucesor)),
                          aplazado.exists()), (2, 0, True))
        self.assertEqual(adaptador.cancelaciones_en_curso(),
                         {str(r.relative_to(adaptador.estado)) for r in registros})
        self.assert_avisos_cancelacion(tren, 2, False, 'suite-sin-leer')
        self.liberar_lanzadores()
        self.assert_canceladas(registros)
        self.tick()
        self.assert_sucesor_sin_candados(sucesor)
        self.assertFalse(aplazado.exists())

    def test_confirmadas_de_tren_activo_siguen_en_curso(self):
        tren, registros, _ = self.lote_con_confirmadas()
        self.tick()
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertTrue(Jale(self.lanzador).suite_con_coordinador(tren.id))
        for registro in registros:
            self.assertFalse((registro / 'cancelar.json').exists())
            self.assertTrue(corrida_en_curso(registro))
        self.assertEqual(self.eventos('suite-sin-leer'), [])

    def test_sondeo_rojo_tardio_recorta_la_espera_de_cancelacion(self):
        tren, registros = self.lote_con_prelanzadas()
        (self.raiz / 'retener-cancelacion').touch()
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        cli = activo.cli
        args = cli.parser().parse_args(['tick', '--repo', REPO, '--config', str(self.privada)])
        segundos = 0
        ci_original = self.contexto.observador.ci

        def dormir(duracion):
            nonlocal segundos
            segundos += duracion

        def ci(repo, commit, rama=None):
            if commit == tren.fase.candidato.C and rama is None:
                if segundos == 80:
                    dormir(11)  # El cuarto sondeo devuelve rojo a los 91 s.
                if segundos >= 91:
                    return ResultadoCI('rojo', ('check',))
            return ci_original(repo, commit, rama)

        reloj = SimpleNamespace(monotonic=lambda: segundos, sleep=dormir,
                                time=lambda: next(self.reloj))
        with patch.object(cli, 'ARRANQUE', 0), patch.object(cli, 'time', reloj), \
                patch.object(cli, 'cuota', return_value=True), \
                patch('adaptadores.jale.time', reloj), \
                patch.object(self.contexto.observador, 'ci', side_effect=ci), \
                patch.object(cli, 'GitHubGit', wraps=cli.GitHubGit,
                             return_value=self.contexto.observador), \
                patch('git_ops.GitTren', return_value=self.contexto.git), \
                patch('operativo.EscritorGitHub', return_value=self.contexto.escritor):
            pasos = list(cli.ticks(args))

        self.assertTrue(all(codigo == 0 for codigo, _ in pasos), pasos)
        self.assertLess(segundos, 120, f'el tick terminó a los {segundos:g} s')
        predecesor, sucesor = self.estado().trenes
        self.assertEqual(predecesor.fase, Terminal('REARMAR'))
        self.assertIsInstance(sucesor.fase, EsperandoCI)
        self.assert_avisos_cancelacion(tren, 2, False)
        for registro in registros:
            self.assertTrue((registro / 'cancelar.json').exists())
            self.esperar_archivo(registro / 'term')
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
        aplazado = Jale(self.lanzador).archivo_prelanzamiento_aplazado(sucesor.id)
        self.assertTrue(aplazado.exists())
        self.assertFalse(any(r.exists() for r in self.registros_de_suite(sucesor)))

        (self.raiz / 'salir').touch()
        for registro in registros:
            self.esperar_candado_libre(registro)
        self.tick()
        self.assert_sucesor_sin_candados(sucesor)
        self.assertFalse(aplazado.exists())

    def test_rearmar_cancela_prelanzadas_vivas(self):
        tren, registros = self.lote_con_prelanzadas()
        c = tren.fase.candidato
        cert = Certificado(c.C, c.arbol, 'suite.json', 'ci', c.B)
        self.registrar_fase(tren, Promoviendo(cert), efectos=(efecto_promover(tren.id, 1),))
        # Se inyecta la lectura del CAS para alcanzar REARMAR con una corrida
        # sin confirmar, que una promoción ordinaria ya habría recogido.
        with patch.object(self.contexto.git, 'reconciliar', return_value=EscritorExterno(
                c.B, c.C, 'a' * 40)):
            self.tick()
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assert_canceladas(registros)
        self.assert_avisos_cancelacion(tren, 2, True)

    def test_juicio_aborta_prelanzadas_y_una_salida_de_transporte_no_reintenta(self):
        tren, registros = self.lote_con_prelanzadas()
        (self.raiz / 'codigo-cancelacion').write_text('255')
        self.reloj = count(4000)
        self.tick()
        self.abortar(tren)
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assert_canceladas(registros, codigo=255)
        self.assert_avisos_cancelacion(tren, 2, True)

    def test_aborto_cancela_tanto_la_suite_confirmada_como_la_especulativa(self):
        tren, (contract, e2e) = self.lote_con_prelanzadas()
        # La configuración cambia entre el prelanzamiento y el verde de CI.
        self.contexto.config['modos'] = ['contract']
        self.configurar(True)
        (self.raiz / 'codigo-cancelacion').write_text('143')
        self.ci = 'verde'
        resultado = self.tick()
        [fila] = [f for f in resultado['outbox'] if f['efecto']['tipo'] == 'iniciar-suite']
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertTrue((contract / 'confirmada.json').exists())
        self.assertFalse((e2e / 'confirmada.json').exists())
        self.assertTrue(all(lanzamiento_activo(r / 'candado') for r in (contract, e2e)))

        (self.raiz / 'pr-cerrado-1').touch()
        self.tick()
        self.abortar(tren)
        with patch('adaptadores.jale.leer_salida') as leer:
            self.tick()
            self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
            self.assert_canceladas([contract, e2e], codigo=143)
            self.tick()
            leer.assert_not_called()
        self.assertEqual(self.eventos('suite-sin-leer'), [dict(
            tren=tren.id, modo='contract', terminada=False,
            cancelacion_pedida=True, cancelacion_confirmada=True,
            clave=json.loads((contract / 'iniciado.json').read_text())['clave'])])
        self.assert_avisos_cancelacion(tren, 1, True)
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer')[0]['modo'], 'e2e')
        self.assertFalse(any((r.parent / 'completo.json').exists() for r in (contract, e2e)))
        self.assert_outbox_cancelado(clave('suite', tren.id))

    def test_juicio_aborta_repeticiones_adelantadas(self):
        self.instalar_lanzador_cancelable()
        self.configurar(False)
        tren, _ = self.hasta(EsperandoCI)
        c = tren.fase.candidato
        suite = self.raiz / 'suite.json'
        suite.write_text(json.dumps([dict(sha=c.C, modo='contract',
                                          unidades=[dict(unidad=jale.UNIDAD)])]))
        fase = Diagnosticando(c, (jale.FALLO,), str(suite), presupuesto_s=100)
        prs = tuple(pr for pr, _, _ in c.eslabones)
        _, solicitud = planificar(fase.fallos, prs, (), fase.presupuesto_s).tanda
        adaptador = Jale(self.lanzador, repeticiones_paralelas=2)
        adaptador.adelantar_repeticiones(c, str(suite), ((solicitud, c.B),), tren.id)
        raiz = adaptador.estado / clave('repetir', tren.id, solicitud.id)
        registros = [raiz / n / '0' for n in ('1', '2')]
        for registro in registros:
            self.esperar_archivo(registro / 'listo')
        self.registrar_fase(tren, fase, Pausa('necesita-juicio',
                                               detalle='confirmacion-candidato:' + jale.UNIDAD))
        self.abortar(tren)
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assert_canceladas(registros)
        self.assert_avisos_cancelacion(tren, 2, True)

    def test_coordinadores_terminados_no_reciben_una_solicitud(self):
        tren, _ = self.hasta(EsperandoCI)
        self.ci = 'rojo'
        self.assert_sin_leer_por_modo(tren)
        self.assertTrue(all(not (r / 'cancelar.json').exists() for r in
                            self.registros_de_suite(tren)))

    def test_codigo_aparece_entre_la_captura_del_aviso_y_la_marca(self):
        tren, registros, efecto = self.lote_con_confirmadas()
        self.abortar_con_head_cambiado(tren)

        def terminar_antes_de_marcar(registro):
            self.liberar_lanzadores()
            self.assertTrue((registro / 'codigo.json').exists())
            return False

        with patch('adaptadores.jale.con_candado', side_effect=terminar_antes_de_marcar) as sondeo:
            self.tick()
        self.assertEqual(sondeo.call_count, 2)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        for registro in registros:
            self.assertFalse((registro / 'cancelar.json').exists())
        avisos = self.eventos('suite-sin-leer')
        self.assertCountEqual([a['modo'] for a in avisos], ['contract', 'e2e'])
        for aviso in avisos:
            self.assertFalse(aviso['terminada'])
            self.assertFalse(aviso['cancelacion_pedida'])
            self.assertFalse(aviso['cancelacion_confirmada'])
            self.assertNotIn('error_cancelacion', aviso)
        self.assert_outbox_cancelado(efecto)

    def test_coordinador_ausente_deja_marca_y_registra_el_error(self):
        tren, _ = self.hasta(EsperandoCI)
        registros = self.registros_de_suite(tren)
        for registro in registros:
            self.esperar_archivo(registro / 'codigo.json')
            self.esperar_candado_libre(registro)
            (registro / 'codigo.json').rename(registro / 'codigo-anterior.json')
        self.ci = 'rojo'
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        avisos = self.eventos('suite-prelanzada-sin-leer')
        self.assertEqual([a['error_cancelacion'] for a in avisos], ['coordinador-ausente'] * 2)
        self.assertTrue(all((r / 'cancelar.json').exists() for r in registros))

    def test_error_de_permiso_al_cancelar_no_frena_el_cierre_ni_el_otro_modo(self):
        tren, registros = self.lote_con_prelanzadas()
        bloqueado, otro = registros

        def guardar_sin_permiso(ruta, valor):
            if ruta == bloqueado / 'cancelar.json':
                raise PermissionError()
            guardar(ruta, valor)

        self.ci = 'rojo'
        with patch('adaptadores.jale.guardar', side_effect=guardar_sin_permiso), \
                patch('adaptadores.jale.PLAZO_CANCELACION_S', .5):
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        avisos = {a['modo']: a for a in self.eventos('suite-prelanzada-sin-leer')}
        self.assertEqual(avisos['contract']['error_cancelacion'], 'PermissionError')
        self.assertFalse(avisos['contract']['cancelacion_pedida'])
        self.assertFalse((bloqueado / 'term').exists())
        self.assert_canceladas([otro])

    def test_aborto_por_juicio_registra_los_modos_sin_leer(self):
        tren, _ = self.hasta(EsperandoCI)
        self.reloj = count(4000)
        self.tick()
        self.abortar(tren)
        self.assert_sin_leer_por_modo(tren)

    def test_aborto_despues_de_consumir_75_no_registra_corrida_sin_leer(self):
        self.plan([{'codigo': 75}])
        self.hasta(EsperandoCI)
        self.ci = 'verde'
        tren, _ = self.hasta(Corriendo)
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(self.estado(), pausa=Pausa(
                'infraestructura', detalle='tres-reanudaciones-agotadas')), (), next(self.reloj))
        self.abortar(tren)
        self.hasta(Terminal)
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), [])

    def test_pausa_o_miembro_retenido_impide_publicar_y_prelanzar(self):
        self.hasta(Armado)

        def detener(nombre, efecto):
            if nombre == 'paso-guardado':
                raise InterruptedError()

        with self.assertRaises(InterruptedError):
            self.tick(detener)
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(self.estado(), pausa=Pausa('manual')), (), next(self.reloj))
        self.tick()
        self.assertEqual(self.invocaciones(), [])
        self.assertEqual(self.lineas('pushes.log'), [])
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(self.estado(), pausa=None), (), next(self.reloj))
        (self.raiz / 'apto-1.json').write_text('{}')
        self.tick()
        self.assertEqual(self.invocaciones(), [])
        self.assertEqual(self.lineas('pushes.log'), [])
