"""U3: prelanzamiento especulativo con un productor de recibos sintético."""
from dataclasses import replace
from itertools import count
import json
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import test_activo as activo
import test_jale as jale
from test_hasito import CANDIDATO
from test_tren import Aislado, estado, observacion
from adaptadores import seleccionar
from adaptadores.hasito import Hasito, ResultadoInvalido
from adaptadores.jale import Jale, PLAZO_PRELANZAMIENTO_S
from dominio import (Armado, Certificado, Corriendo, Efecto, EsperandoCI, Pausa, ResultadoCI,
                     Terminal, Tren, clave)
from efectos import EfectoFallido, ejecutar
from git_ops import ErrorGit, Publicado, RamaOcupada
from operador import FalloSnapshot, lanzamiento_activo

REPO = activo.REPO


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
        return self.adaptador.suite(CANDIDATO, 'lote', permitir_lanzar=permitir)

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
                ('candado', None, (False, False, True), 1),
                ('salida', (None, 0), (False, False), 1),
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

    def hasta(self, fase):
        for _ in range(8):
            resultado = self.tick()
            trenes = self.estado().trenes
            if trenes and isinstance(trenes[0].fase, fase):
                return trenes[0], resultado
        self.fail('fase no alcanzada: ' + fase.__name__)

    def eventos(self, tipo):
        with self.ledger() as ledger:
            return [json.loads(e['payload']) for e in ledger.ultimos_eventos(REPO)
                    if e['tipo'] == tipo]

    def test_tick_publica_y_prelanza_con_ci_pendiente_y_recoge_en_verde(self):
        self.plan([{'esperar': True}, {}])
        _, resultado = self.hasta(EsperandoCI)
        self.assertTrue(any(':refs/heads/tren/' in p for p in self.lineas('pushes.log')))
        self.assertTrue(any(f['efecto']['tipo'] == 'solicitar-ci' and f['estado'] == 'resultado'
                            for f in resultado['outbox']))
        self.esperar_archivo(self.raiz / 'listo')
        self.assertEqual([fila[2] for fila in self.lanzamientos()], ['contract'])
        self.ci = 'verde'
        (self.raiz / 'seguir').touch()
        resultado = self.tick()
        self.assertTrue(any(f['efecto']['tipo'] == 'iniciar-suite' and f['estado'] == 'resultado'
                            for f in resultado['outbox']))
        self.hasta(Certificado)
        self.assertEqual([fila[2] for fila in self.lanzamientos()], ['contract', 'e2e'])
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

    def assert_sin_leer_una_vez(self, tren):
        registro = Jale(self.lanzador).estado / clave('suite', tren.id) / 'contract' / '0'
        self.esperar_archivo(registro / 'codigo.json')
        iniciado = json.loads((registro / 'iniciado.json').read_text())
        self.hasta(Terminal)
        esperado = [dict(tren=tren.id, clave=iniciado['clave'], terminada=True)]
        self.assertEqual(self.eventos('suite-prelanzada-sin-leer'), esperado)
        self.tick()
        with self.ledger() as ledger:
            filas = ledger.db.execute("SELECT payload FROM events WHERE repo=? AND tipo=?",
                                      (REPO, 'suite-prelanzada-sin-leer'))
            self.assertEqual([json.loads(fila[0]) for fila in filas], esperado)

    def test_ci_rojo_registra_una_sola_corrida_sin_leer(self):
        tren, _ = self.hasta(EsperandoCI)
        self.ci = 'verde'

        def detener(nombre, efecto):
            if nombre == 'antes-ejecutar' and efecto.tipo == 'iniciar-suite':
                raise InterruptedError()

        with self.assertRaises(InterruptedError):
            self.tick(detener)
        self.ci = 'rojo'
        self.assert_sin_leer_una_vez(tren)

    def abortar(self, tren):
        self.cli('juicio', '--tren', tren.id, '--decision', 'abortar', '--motivo', 'prueba')

    def test_aborto_por_juicio_registra_una_sola_corrida_sin_leer(self):
        tren, _ = self.hasta(EsperandoCI)
        self.reloj = count(4000)
        self.tick()
        self.abortar(tren)
        self.assert_sin_leer_una_vez(tren)

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
