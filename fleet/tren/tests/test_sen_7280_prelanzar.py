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
from adaptadores.ejecucion_jale import guardar
from adaptadores.hasito import Hasito, ResultadoInvalido
from adaptadores.jale import Jale, PLAZO_PRELANZAMIENTO_S
from atribucion import planificar
from dominio import (Armado, Certificado, Corriendo, Diagnosticando, Efecto, EscritorExterno,
                     EsperandoCI, Pausa, Promoviendo, ResultadoCI, Terminal, Tren, clave,
                     efecto_promover)
from efectos import EfectoFallido, ejecutar
from git_ops import ErrorGit, Publicado, RamaOcupada
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

    def test_ci_rojo_registra_los_modos_sin_leer(self):
        tren, _ = self.hasta(EsperandoCI)
        self.ci = 'verde'

        def detener(nombre, efecto):
            if nombre == 'antes-ejecutar' and efecto.tipo == 'iniciar-suite':
                raise InterruptedError()

        with self.assertRaises(InterruptedError):
            self.tick(detener)
        self.ci = 'rojo'
        self.assert_sin_leer_por_modo(tren)

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

    def registros_de_suite(self, tren):
        raiz = Jale(self.lanzador).estado / clave('suite', tren.id)
        return [raiz / modo / '0' for modo in ('contract', 'e2e')]

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

    def assert_canceladas(self, registros, codigo=75):
        for registro in registros:
            self.assertTrue((registro / 'term').exists())
            self.assertEqual(json.loads((registro / 'codigo.json').read_text()), codigo)
            self.assertFalse(lanzamiento_activo(registro / 'candado'))
            self.assertEqual((registro / 'invocaciones').read_text().splitlines(), ['1'])

    def assert_avisos_cancelacion(self, tren, cantidad, confirmada):
        avisos = [a for a in self.eventos('suite-prelanzada-sin-leer') if a['tren'] == tren.id]
        self.assertEqual(len(avisos), cantidad)
        for aviso in avisos:
            self.assertTrue(aviso['cancelacion_pedida'])
            self.assertEqual(aviso['cancelacion_confirmada'], confirmada)

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

    def test_partido_cancela_y_suelta_antes_de_prelanzar_sucesor(self):
        tren, registros = self.lote_con_prelanzadas()
        self.ci = 'rojo'
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('PARTIDO'))
        self.assert_canceladas(registros)
        self.assert_avisos_cancelacion(tren, 2, True)
        adaptador = Jale(self.lanzador, prelanzar=True)
        adaptador.prelanzar(tren.fase.candidato, tren.id)
        with self.assertRaisesRegex(ResultadoInvalido, 'corrida-interrumpida'):
            adaptador.suite(tren.fase.candidato, tren.id)
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        self.assert_sucesor_sin_candados(self.esperar_sucesor_ci(tren))

    def test_partido_aplaza_con_candado_y_prelanza_en_el_tick_siguiente_a_la_salida(self):
        tren, registros = self.lote_con_prelanzadas()
        (self.raiz / 'retener-cancelacion').touch()
        self.ci = 'rojo'
        with patch('adaptadores.jale.PLAZO_CANCELACION_S', .5):
            self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('PARTIDO'))
        self.assert_avisos_cancelacion(tren, 2, False)
        for registro in registros:
            self.esperar_archivo(registro / 'term')
            self.assertTrue(lanzamiento_activo(registro / 'candado'))
        (self.raiz / 'predecesor.json').write_text(json.dumps(list(map(str, registros))))
        sucesor = self.esperar_sucesor_ci(tren)
        self.assertTrue(all(not r.exists() for r in self.registros_de_suite(sucesor)))
        self.assertEqual(sum(':refs/heads/tren/' in p for p in self.lineas('pushes.log')), 2)
        (self.raiz / 'salir').touch()
        for registro in registros:
            self.esperar_archivo(registro / 'codigo.json')
            self.esperar_candado_libre(registro)
        self.tick()
        self.assert_sucesor_sin_candados(sucesor)
        self.assertEqual(sum(':refs/heads/tren/' in p for p in self.lineas('pushes.log')), 2)

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

    def test_pid_sin_candado_no_recibe_una_senal(self):
        tren, _ = self.hasta(EsperandoCI)
        registros = self.registros_de_suite(tren)
        for registro in registros:
            self.esperar_archivo(registro / 'codigo.json')
            self.esperar_candado_libre(registro)
            (registro / 'codigo.json').rename(registro / 'codigo-anterior.json')
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)']) as ajeno:
            try:
                for registro in registros:
                    (registro / 'pid.json').write_text(json.dumps(ajeno.pid))
                self.ci = 'rojo'
                self.tick()
                self.assertIsNone(ajeno.poll())
                self.assertEqual(self.estado().trenes[0].fase, Terminal('PARTIDO'))
                self.assertEqual(self.eventos('suite-prelanzada-sin-leer')[0]['error_cancelacion'],
                                 'coordinador-ausente')
                self.assertTrue(all((r / 'cancelar.json').exists() for r in registros))
            finally:
                ajeno.terminate()

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
        self.assertEqual(self.estado().trenes[0].fase, Terminal('PARTIDO'))
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
