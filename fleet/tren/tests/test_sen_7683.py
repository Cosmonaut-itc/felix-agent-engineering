"""Reserva de Boat con tick, ledger y un ejecutable falso, sólo en el host de pruebas."""
from contextlib import closing, contextmanager
from dataclasses import replace
from functools import partial
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_hasito import GitFalso
from test_tren import Aislado, BASE, HEAD, REPO, cli, entrega, observacion
from adaptadores import seleccionar
from adaptadores.jale import Jale
from dominio import (Admitido, Aterrizado, Candidato, Certificado, Corriendo, EnBase, Entrada,
                     EscritorExterno, EsperandoCI, Estado, Incierto, Pausa, Promoviendo,
                     Terminal, Tren, datos, recibo_ci, separar_devueltos)
from observador import Falso
from operador import FalloSnapshot


CANDIDATO = Candidato(BASE, ((1, HEAD, 'e' * 40),), 'c' * 40, 'd' * 40)


class ReservaBoatTests(Aislado):
    def setUp(self):
        super().setUp()
        config = self.raiz / 'config'
        entorno = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)})
        entorno.start()
        self.addCleanup(entorno.stop)
        inscritos = config / 'pao-tren/inscritos'
        inscritos.parent.mkdir(parents=True)
        inscritos.write_text(REPO + ' activo\n')
        self.registro = self.ledger()
        self.entrega = entrega()
        self.registro.entregar(self.entrega, 1)
        self.observador = Falso(observacion())
        self.log = self.raiz / 'pool.jsonl'
        self.binario = self.raiz / 'bin falso' / 'boat-pool'
        self.escribir_pool(self.binario)
        self.observador.config = dict(adaptador='jale', host='boat',
            lanzador=str(self.raiz / 'lanzador-no-disponible'), boat_pool=str(self.binario))
        self.git = GitFalso(self.raiz)
        self.git.reconciliar = Mock(return_value=EnBase(CANDIDATO.B, CANDIDATO.C))
        self.escritor = Mock()
        self.escritor.ejecutar_feedback.return_value = {}
        reloj = patch('operativo.time', wraps=time)
        self.reloj = reloj.start()
        self.addCleanup(reloj.stop)

    def escribir_pool(self, binario, final=''):
        binario.parent.mkdir(parents=True, exist_ok=True)
        programa = binario.with_suffix('.py')
        sqlite = self.registro.directorio / 'ledger.sqlite3'
        programa.write_text(f"""import json
import sqlite3
import sys
import time

with sqlite3.connect({str(sqlite)!r}) as db:
    fila = db.execute("SELECT instante, payload FROM events WHERE tipo='operativo' "
                      "ORDER BY seq DESC LIMIT 1").fetchone()
instante, estado = (fila[0], json.loads(fila[1])['estado']) if fila else (None, None)
with open({str(self.log)!r}, 'a') as log:
    print(json.dumps([sys.argv[1:], instante, estado]), file=log)
{final}
""")
        binario.write_text('#!/bin/sh\nexec ' +
            shlex.join((sys.executable, '-I', '-B', str(programa))) + ' "$@"\n')
        binario.chmod(0o700)

    def guardar(self, fase, pausa=None, entregas=None):
        entregas = entregas if entregas is not None else (self.entrega,)
        tren = Tren('lote-prueba', tuple(e.recibo for e in entregas), fase)
        entradas = tuple(Entrada(e, i, Admitido(), i, tren=tren.id)
                         for i, e in enumerate(entregas, 1))
        self.registro.guardar_paso(Estado(REPO, entradas, (tren,), pausa), (), 1)
        return tren

    def guardar_lote_doble(self):
        segunda = entrega(2)
        self.registro.entregar(segunda, 2)
        candidato = replace(CANDIDATO, eslabones=CANDIDATO.eslabones + ((2, HEAD, 'f' * 40),))
        self.observador.observacion = observacion(2)
        return self.guardar(EsperandoCI(candidato, 1), entregas=(self.entrega, segunda))

    def estado(self):
        return self.registro.estado(REPO, operativo=True)

    def cli(self, comando, *argv, **kwargs):
        args = cli.parser().parse_args([comando, '--repo', REPO, *argv])
        salida = cli.ejecutar(args, **kwargs)
        self.assertEqual(salida[0], 0, salida)
        return salida

    def tick(self, ahora=100):
        self.reloj.time.return_value = ahora
        adaptador = seleccionar(self.observador.config, self.git)
        return self.cli('tick', ahora=ahora, observador=self.observador, git=self.git,
                        adaptador=adaptador, escritor=self.escritor)

    def intento_tick(self, ahora):
        args = cli.parser().parse_args(['tick', '--repo', REPO])
        ejecutar = partial(cli.ejecutar, ahora=ahora, observador=self.observador, git=self.git,
                           adaptador=seleccionar(self.observador.config, self.git),
                           escritor=self.escritor)
        self.reloj.time.return_value = ahora
        with patch.object(cli, 'ejecutar', side_effect=ejecutar):
            return cli.intento(args)

    @contextmanager
    def escritor_sqlite(self):
        abrir = cli.Ledger

        def sin_espera():
            ledger = abrir()
            ledger.db.execute('PRAGMA busy_timeout=0')
            return ledger

        ruta = self.registro.directorio / 'ledger.sqlite3'
        with closing(sqlite3.connect(ruta, isolation_level=None)) as otro, \
                patch.object(cli, 'Ledger', side_effect=sin_espera):
            yield otro

    def ver_ci(self, tren, color, n=1):
        self.observador.observacion = observacion(
            n, ci_candidato=color, tren_id=tren.id, ci_recibo=recibo_ci(tren),
            ci_rojos=('check',) if color == 'rojo' else ())

    def llamadas(self):
        return [json.loads(linea) for linea in self.log.read_text().splitlines()] if (
            self.log.exists()) else []

    def assert_llamadas(self, *esperadas):
        self.assertEqual([(argv, instante) for argv, instante, _ in self.llamadas()],
                         list(esperadas))

    def assert_ultimo_estado_guardado(self):
        llamadas = self.llamadas()
        self.assertTrue(llamadas, 'El ejecutable falso no recibió ninguna llamada')
        self.assertEqual(llamadas[-1][2], datos(self.estado()))

    def fallos(self):
        return [json.loads(e['payload']) for e in self.registro.ultimos_eventos(REPO)
                if e['tipo'] == 'fallo-reserva-boat']

    @staticmethod
    def reserva(lote, ahora, clases='contract,e2e', ttl='1800'):
        return (['reservar', '--repo', REPO, '--lote', lote, '--clases', clases,
                 '--ttl', ttl], ahora)

    @staticmethod
    def recorte(lote, ahora, hasta):
        return (['reservar', '--repo', REPO, '--lote', lote, '--clases', 'contract,e2e',
                 '--vence', str(hasta)], ahora)

    @staticmethod
    def liberacion(lote, ahora):
        return (['liberar-reserva', '--repo', REPO, '--lote', lote], ahora)

    def test_lote_nuevo_reserva_y_el_siguiente_tick_renueva(self):
        self.tick()
        lote = self.estado().trenes[0].id
        self.assert_llamadas(self.reserva(lote, 100))
        self.assert_ultimo_estado_guardado()
        self.tick(220)
        self.assert_llamadas(self.reserva(lote, 100), self.reserva(lote, 220))
        self.assert_ultimo_estado_guardado()

    def test_clases_solo_de_modos_configurados_en_boat_y_binario_por_omision(self):
        self.escribir_pool(self.home / '.local/bin/boat-pool')
        config = self.observador.config
        config.pop('boat_pool')
        config['host'] = {'contract': 'desktop', 'e2e': 'boat'}
        lote = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.tick()
        config.update(host={'contract': 'boat', 'e2e': 'boat'}, modos=['contract'])
        self.tick(220)
        self.assert_llamadas(self.reserva(lote, 100, 'e2e'), self.reserva(lote, 220, 'contract'))

    def test_aterrizado_acorta_la_reserva_y_boat_la_vence(self):
        pool, inicio = self.preparar_pool()
        B, C, arbol = CANDIDATO.B, CANDIDATO.C, CANDIDATO.arbol
        tren = self.guardar(Promoviendo(Certificado(C, arbol, 'suite.json', 'verde', B)))
        proceso = self.proceso_pool(pool)
        with patch('operativo.subprocess', proceso):
            self.tick(100)
        pool.api.now = inicio + 120
        self.observador.observacion = observacion(
            main=Aterrizado(B, C, arbol), tren_id=tren.id, intento_promocion=1)
        ejecutar_pool = proceso.run

        def demorar_respuesta(argv, **opciones):
            ejecutar_pool(argv, **opciones)
            pool.api.now += 2

        proceso.run = demorar_respuesta
        with patch('operativo.subprocess', proceso):
            self.tick(220)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ATERRIZADO'))
        pool.api.now = inicio + 419.75
        self.assert_pool(pool, {tren.id}, 75)
        pool.api.now = inicio + 420
        self.assert_pool(pool, set(), 0)
        self.observador.observacion = replace(self.observador.observacion, prs=())
        proceso.run = Mock(wraps=ejecutar_pool)
        with patch('operativo.subprocess', proceso):
            self.tick(520)
            pool.api.now = inicio + 540
            self.tick(640)
        proceso.run.assert_called_once()
        self.assertEqual(proceso.run.call_args.args[0][1:], self.liberacion(tren.id, 520)[0])

    def test_devuelto_por_ci_rojo_acorta_la_reserva(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        self.ver_ci(tren, 'rojo')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
        self.assert_llamadas(self.recorte(tren.id, 100, 400))
        self.assert_ultimo_estado_guardado()

    def test_abortado_al_salir_de_pausa_deja_reserva_corta(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1),
                            Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.cli('juicio', '--tren', tren.id, '--decision', 'abortar', '--motivo', 'prueba')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assertIsNone(self.estado().pausa)
        self.assert_llamadas(self.recorte(tren.id, 100, 400))
        self.assert_ultimo_estado_guardado()

    def test_pausa_sin_lote_vivo_libera_la_reserva_del_relevo(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        self.ver_ci(tren, 'rojo')
        self.tick()
        self.cli('pausa', '--motivo', 'prueba')
        self.tick(220)
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='prueba'))
        self.tick(340)
        self.assert_llamadas(self.recorte(tren.id, 100, 400), self.recorte(tren.id, 220, 400),
                             self.liberacion(tren.id, 220))

    def test_pausa_manual_libera_y_levantar_vuelve_a_reservar(self):
        lote = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.tick()
        self.cli('pausa', '--motivo', 'prueba', ahora=200)
        self.tick(220)
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='prueba', hasta=7420))
        self.assert_ultimo_estado_guardado()
        self.tick(340)
        self.cli('pausa', '--levantar', '--motivo', 'prueba', ahora=400)
        self.tick(460)
        self.assertIsNone(self.estado().pausa)
        self.assert_llamadas(self.reserva(lote, 100), self.liberacion(lote, 220),
                             self.liberacion(lote, 340), self.reserva(lote, 460))

    def test_reanudar_reserva_antes_de_lanzar_la_suite(self):
        lote = self.guardar(Corriendo(CANDIDATO, 'suite.json'),
                            Pausa('infraestructura', detalle='cobertura-parcial-boat')).id
        self.tick()
        self.cli('juicio', '--tren', lote, '--decision', 'reanudar', '--motivo', 'prueba')
        llamadas_al_lanzar = []

        def al_lanzar(*args, **kwargs):
            llamadas_al_lanzar.extend(self.llamadas())

        with patch.object(Jale, 'suite', side_effect=al_lanzar) as suite:
            self.tick(220)
        suite.assert_called_once()
        self.assertEqual(suite.call_args.args[1], lote)
        self.assertTrue(suite.call_args.kwargs['permitir_lanzar'])
        self.assertIsNone(self.estado().pausa)
        self.assert_llamadas(self.liberacion(lote, 100), self.reserva(lote, 220))
        self.assertEqual(llamadas_al_lanzar, self.llamadas())
        self.assert_ultimo_estado_guardado()

    def test_pausa_de_infraestructura_al_drenar_libera_despues_de_guardarla(self):
        with patch.object(self.git, 'armar', side_effect=FalloSnapshot('prueba')):
            self.tick()
        lote = self.estado().trenes[0].id
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='checkout-operador:prueba'))
        self.assert_llamadas(self.reserva(lote, 100), self.liberacion(lote, 100))
        self.assertIsNone(self.llamadas()[0][2]['pausa'])
        self.assert_ultimo_estado_guardado()

    def test_rearmar_reserva_el_lote_nuevo_antes_de_liberar_el_viejo(self):
        viejo = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.tick()
        self.observador.observacion = replace(observacion(), base='f' * 40)
        self.tick(220)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.tick(340)
        nuevo = self.estado().trenes[-1].id
        self.assertNotEqual(nuevo, viejo)
        self.assert_llamadas(self.reserva(viejo, 100), self.recorte(viejo, 220, 520),
                             self.recorte(viejo, 340, 520),
                             self.reserva(nuevo, 340), self.liberacion(viejo, 340))

    def preparar_pool(self):
        pruebas_pool = Path(__file__).resolve().parents[2] / 'boat-pool/tests'
        with patch.object(sys, 'path', [str(pruebas_pool), *sys.path]):
            import test_pool

        repo_pool = patch.object(test_pool, 'REPO', REPO)
        repo_pool.start()
        self.addCleanup(repo_pool.stop)
        pool = test_pool.Harness()
        self.addCleanup(pool.doCleanups)
        with patch.dict(os.environ):
            pool.setUp()
        pool.reservation_vms()
        self.reloj.time.side_effect = lambda: pool.api.now
        return pool, test_pool.NOW

    def comando_pool(self, pool, *args):
        with patch.dict(os.environ, pool.env):
            return pool.run_cli(*args)

    def proceso_pool(self, pool, rechazar=None):
        def ejecutar_pool(argv, **_):
            if (argv[1], argv[5]) == rechazar:
                raise subprocess.CalledProcessError(1, argv)
            codigo, _, error = self.comando_pool(pool, *argv[1:])
            self.assertEqual(codigo, 0, error)

        return SimpleNamespace(
            run=ejecutar_pool, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
            CalledProcessError=subprocess.CalledProcessError, TimeoutExpired=subprocess.TimeoutExpired)

    def proceso_pool_viejo(self, pool):
        self.escribir_pool(self.binario, "if '--vence' in sys.argv[1:]:\n    sys.exit(64)")
        proceso = self.proceso_pool(pool)
        ejecutar_pool = proceso.run

        def ejecutar_viejo(argv, **opciones):
            subprocess.run(argv, **opciones)
            ejecutar_pool(argv, **opciones)

        proceso.run = ejecutar_viejo
        return proceso

    def reservas_pool(self, pool):
        codigo, salida, error = self.comando_pool(pool, 'estado', '--json')
        self.assertEqual(codigo, 0, error)
        return {r['lote'] for r in json.loads(salida)['reservas']}

    def assert_pool(self, pool, reservas, codigo):
        vivas = self.reservas_pool(pool)
        salida, _, error = self.comando_pool(
            pool, *pool.args('ajena-en-relevo', 'e2e', '--prioridad', 'suite'))
        self.assertEqual((vivas, salida), (reservas, codigo), error)
        if codigo == 75:
            self.assertIn('reserva del tren', error)

    def test_partido_protege_el_relevo_frente_a_una_corrida_en_espera(self):
        pool, inicio = self.preparar_pool()
        entorno_tren = {clave: os.environ[clave] for clave in pool.env}

        def cerrar_lote():
            with patch.dict(os.environ, entorno_tren):
                self.ver_ci(tren, 'rojo', n=2)
                self.tick(220)
            self.assertEqual([t.fase for t in self.estado().trenes],
                             [Terminal('REARMAR'), Terminal('PARTIDO')])
            # En main la prueba suelta los arriendos que SEN-7709 liberará al cancelar.
            for arriendo in arriendos:
                self.assertEqual(self.comando_pool(pool, 'soltar', arriendo)[0], 0)

        original = self.guardar_lote_doble()
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick()
            self.ver_ci(original, 'rojo', n=2)
            self.tick(101)
            self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
            self.observador.observacion = observacion(2)
            for ahora in (102, 103, 104):
                self.tick(ahora)
            tren = self.estado().trenes[-1]
            self.assertIsInstance(tren.fase, EsperandoCI)
            self.assertEqual(tren.fase.solicitud, 1)
            self.assertEqual(tren.recibos, original.recibos)
            self.assertEqual(self.reservas_pool(pool), {tren.id})
            with patch.dict(os.environ, pool.env):
                arriendos = [pool.get_lease('anterior-' + modo, modo, '--prioridad', 'tren')
                             for modo in ('contract', 'e2e')]
            pool.api.now = inicio + 119.75
            with pool.resume_clock({120: cerrar_lote}):
                en_hueco, _, razon = self.comando_pool(pool, *pool.args(
                    'ajena-e2e', 'e2e', '--prioridad', 'suite', '--esperar', '0.5'))

            # La observación aún carece de base en el primer tick del relevo.
            self.observador.observacion = replace(observacion(2), base='')
            pool.api.now = inicio + 240
            self.tick(340)
            self.assertEqual(len(self.estado().trenes), 2)
            en_tick_vacio, _, razon_tick_vacio = self.comando_pool(
                pool, *pool.args('ajena-tick-vacio', 'e2e', '--prioridad', 'suite'))

            self.observador.observacion = observacion(2)
            pool.api.now = inicio + 360
            self.tick(460)
            anterior, sucesor = self.estado().trenes[-2:]
            self.assertEqual(anterior.id, tren.id)
            self.assertNotIsInstance(sucesor.fase, Terminal)
            self.assertEqual(sucesor.recibos, tren.recibos[:1])

        with patch.dict(os.environ, pool.env), pool.resume_clock({}):
            pool.get_lease('sucesor-contract', 'contract', '--prioridad', 'tren')
            pedir_sucesor, _, error = pool.run_cli(*pool.args(
                'sucesor-e2e', 'e2e', '--prioridad', 'tren', '--esperar', '180'))
        self.assertEqual(dict(EN_HUECO=en_hueco, PEDIR_SUCESOR=pedir_sucesor),
                         dict(EN_HUECO=75, PEDIR_SUCESOR=0), (razon, error))
        self.assertIn('reserva del tren', razon)
        self.assertEqual(en_tick_vacio, 75, razon_tick_vacio)
        self.assertIn('reserva del tren', razon_tick_vacio)
        self.assertEqual(self.reservas_pool(pool), {sucesor.id})

    def iniciar_cierre_lento(self):
        pool, inicio = self.preparar_pool()
        tren = self.guardar_lote_doble()
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(100)
        self.ver_ci(tren, 'rojo', n=2)
        pool.api.now = inicio + 120
        return pool, inicio, tren.id

    def cerrar_lento(self, pool, proceso):
        observar = self.observador.observar

        def observar_cierre(estado, ahora):
            pool.api.now += 90
            return observar(estado, ahora)

        with patch('operativo.subprocess', proceso), \
                patch.object(self.observador, 'observar', side_effect=observar_cierre):
            return self.intento_tick(220)

    def assert_vencimiento_cierre_lento(self, pool, inicio, lote):
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.reservas_pool(pool), {lote})
        self.observador.observacion = replace(observacion(2), prs=())
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            for ahora in (340, 460, 580):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
        self.assert_pool(pool, {lote}, 75)
        pool.api.now = inicio + 509.75  # El cierre real fue en t=310.
        self.assert_pool(pool, {lote}, 75)
        pool.api.now = inicio + 510
        self.assert_pool(pool, set(), 0)

    def test_cierre_lento_cuenta_desde_el_fin_de_la_observacion(self):
        pool, inicio, lote = self.iniciar_cierre_lento()
        codigo, resultado = self.cerrar_lento(pool, self.proceso_pool(pool))
        self.assertEqual(codigo, 0, resultado)
        self.assert_vencimiento_cierre_lento(pool, inicio, lote)

    def test_timeout_despues_de_aplicar_el_recorte(self):
        pool, inicio, lote = self.iniciar_cierre_lento()
        proceso = self.proceso_pool(pool)
        ejecutar_pool = proceso.run

        def perder_respuesta(argv, **opciones):
            ejecutar_pool(argv, **opciones)
            raise subprocess.TimeoutExpired(argv, opciones['timeout'])

        proceso.run = perder_respuesta
        codigo, resultado = self.cerrar_lento(pool, proceso)
        self.assertEqual(codigo, 0, resultado)
        self.assertEqual(self.fallos(), [dict(lote=lote, comando='reservar', clase='TimeoutExpired')])
        self.assert_vencimiento_cierre_lento(pool, inicio, lote)

    def test_sqlite_ocupado_despues_de_aplicar_el_recorte(self):
        pool, inicio, lote = self.iniciar_cierre_lento()
        proceso = self.proceso_pool(pool)
        ejecutar_pool = proceso.run

        with self.escritor_sqlite() as otro:
            def ocupar_al_responder(argv, **opciones):
                ejecutar_pool(argv, **opciones)
                otro.execute('BEGIN IMMEDIATE')

            proceso.run = ocupar_al_responder
            codigo, resultado = self.cerrar_lento(pool, proceso)
        self.assertEqual(codigo, 0, resultado)
        self.assert_vencimiento_cierre_lento(pool, inicio, lote)

    def test_sqlite_ocupado_al_guardar_el_relevo_conserva_el_primer_vencimiento(self):
        pool, inicio, lote = self.iniciar_cierre_lento()
        proceso = self.proceso_pool(pool)
        proceso.run = Mock(wraps=proceso.run)
        clase_ledger = type(self.registro)
        evento = clase_ledger.evento
        self.reloj.monotonic.return_value = 0

        with self.escritor_sqlite() as otro:
            def ocupar_al_guardar(ledger, repo, tipo, instante, payload):
                if tipo != 'relevo-boat':
                    return evento(ledger, repo, tipo, instante, payload)
                otro.execute('BEGIN IMMEDIATE')
                try:
                    return evento(ledger, repo, tipo, instante, payload)
                finally:
                    otro.execute('ROLLBACK')
                    self.reloj.monotonic.return_value = 30

            with patch.object(clase_ledger, 'evento', autospec=True, side_effect=ocupar_al_guardar):
                codigo, resultado = self.cerrar_lento(pool, proceso)
        self.assertEqual(codigo, 0, resultado)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assertEqual(self.registro.relevos_boat(REPO), {})
        proceso.run.assert_called_once()
        self.assertEqual(proceso.run.call_args.args[0][1:], self.recorte(lote, 220, inicio + 510)[0])
        self.assert_pool(pool, {lote}, 75)

        proceso.run.reset_mock()
        self.observador.observacion = replace(observacion(2), prs=())
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', proceso):
            self.tick(340)
        proceso.run.assert_called_once()
        self.assertEqual(proceso.run.call_args.args[0][1:], self.recorte(lote, 340, inicio + 540)[0])
        pool.api.now = inicio + 509.75
        self.assert_pool(pool, {lote}, 75)
        pool.api.now = inicio + 510
        self.assertEqual(self.reservas_pool(pool), set())
        with patch('operativo.subprocess', proceso):
            self.tick(610)
        self.assert_pool(pool, set(), 0)

    def test_reanudar_no_recrea_un_relevo_ya_liberado(self):
        pool, inicio = self.preparar_pool()
        C, arbol, B = CANDIDATO.C, CANDIDATO.arbol, CANDIDATO.B
        tren = self.guardar(Promoviendo(Certificado(C, arbol, 'suite.json', 'verde', B)))
        main_ajeno = 'f' * 40
        self.reloj.monotonic.return_value = 0
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(100)
            self.assertEqual(self.reservas_pool(pool), {tren.id})
            pool.api.now = inicio + 120
            self.observador.observacion = observacion(
                main=EscritorExterno(B, C, main_ajeno), tren_id=tren.id, intento_promocion=1)

            clase_ledger = type(self.registro)
            evento = clase_ledger.evento
            with self.escritor_sqlite() as lector:
                def evento_con_lector(ledger, repo, tipo, instante, payload):
                    if tipo == 'relevo-boat':
                        lector.execute('BEGIN')
                        lector.execute('SELECT COUNT(*) FROM events').fetchone()
                    return evento(ledger, repo, tipo, instante, payload)

                with patch.object(clase_ledger, 'evento', autospec=True, side_effect=evento_con_lector):
                    codigo, salida = self.intento_tick(220)

            self.assertEqual(codigo, 0, salida)
            self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
            self.assertEqual(self.estado().pausa, Pausa('escritor-externo', main=main_ajeno))
            self.assertEqual(self.registro.relevos_boat(REPO), {})
            self.assertEqual(self.reservas_pool(pool), set())

            self.observador.observacion = replace(observacion(), base='', prs=())
            self.cli('juicio', '--tren', tren.id, '--decision', 'reanudar', '--motivo', 'prueba',
                     ahora=330)
            pool.api.now = inicio + 240
            self.tick(340)
            estado = self.estado()
            self.assertIsNone(estado.pausa)
            self.assertEqual([t.fase for t in estado.trenes], [Terminal('REARMAR')])
            self.assertEqual(self.registro.relevos_boat(REPO), {tren.id: inicio + 540})
            pool.api.now = inicio + 360
            self.tick(460)

        pool.api.now = inicio + 420
        self.assert_pool(pool, set(), 0)

    def test_sqlite_ocupado_al_registrar_el_fallo_conserva_la_salida_del_tick(self):
        pool, inicio, lote = self.iniciar_cierre_lento()
        proceso = self.proceso_pool(pool)
        ejecutar_pool = proceso.run

        with self.escritor_sqlite() as otro:
            def ocupar_al_perder_respuesta(argv, **opciones):
                ejecutar_pool(argv, **opciones)
                otro.execute('BEGIN IMMEDIATE')
                raise subprocess.TimeoutExpired(argv, opciones['timeout'])

            proceso.run = ocupar_al_perder_respuesta
            codigo, resultado = self.cerrar_lento(pool, proceso)
        self.assertEqual(codigo, 0, resultado)
        self.assertEqual(self.fallos(), [])
        self.assert_vencimiento_cierre_lento(pool, inicio, lote)

    def test_fallo_al_acortar_reintenta_con_el_vencimiento_original(self):
        pool, inicio = self.preparar_pool()
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick()
            self.assertEqual(self.reservas_pool(pool), {tren.id})
            pool.api.now = inicio + 120
            self.ver_ci(tren, 'rojo')
            with patch('operativo.subprocess', self.proceso_pool(pool, ('reservar', tren.id))):
                self.tick(220)
            self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
            self.assertEqual(self.fallos(), [dict(lote=tren.id, comando='reservar',
                                                  clase='CalledProcessError', codigo=1)])

            for ahora in (340, 460):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
                self.assert_pool(pool, {tren.id}, 75)
        self.assertIsNone(self.estado().pausa)
        relevos = [json.loads(e['payload']) for e in self.registro.ultimos_eventos(REPO)
                   if e['tipo'] == 'relevo-boat']
        self.assertEqual(relevos, [dict(lote=tren.id, hasta=inicio + 420)])
        pool.api.now = inicio + 419.75
        self.assert_pool(pool, {tren.id}, 75)
        pool.api.now = inicio + 420
        self.assert_pool(pool, set(), 0)

    def test_pool_caido_hasta_vencer_el_relevo_libera_al_recuperarse(self):
        pool, inicio = self.preparar_pool()
        tren = self.guardar_lote_doble()
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(100)
        self.ver_ci(tren, 'rojo', n=2)
        proceso = self.proceso_pool(pool, ('reservar', tren.id))
        with patch('operativo.subprocess', proceso):
            pool.api.now = inicio + 120
            self.tick(220)
            self.observador.observacion = replace(observacion(2), prs=())
            for ahora in (340, 460):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
        self.assertEqual(len(self.fallos()), 3)

        pool.api.now = inicio + 420
        with patch('operativo.subprocess', self.proceso_pool(pool, ('liberar-reserva', tren.id))):
            self.tick(520)
        self.assertEqual(self.fallos()[0], dict(lote=tren.id, comando='liberar-reserva',
                                               clase='CalledProcessError', codigo=1))
        self.assert_pool(pool, {tren.id}, 75)

        pool.api.now = inicio + 480
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(580)
        self.assert_pool(pool, set(), 0)
        self.assertEqual(self.registro.relevos_boat(REPO), {tren.id: None})

    def test_deja_de_reintentar_la_liberacion_al_cumplirse_el_ttl_largo(self):
        pool, inicio = self.preparar_pool()
        tren = self.guardar(Terminal('DEVUELTO'))
        self.registro.evento(REPO, 'relevo-boat', inicio + 120,
                             {'lote': tren.id, 'hasta': inicio + 420})
        self.observador.observacion = replace(observacion(), prs=())
        proceso = self.proceso_pool(pool, ('liberar-reserva', tren.id))
        proceso.run = Mock(wraps=proceso.run)
        with patch('operativo.subprocess', proceso):
            pool.api.now = inicio + 2219
            self.tick(2319)
            pool.api.now = inicio + 2220
            self.tick(2320)
        proceso.run.assert_called_once()
        self.assertEqual(proceso.run.call_args.args[0][1:], self.liberacion(tren.id, 2319)[0])

    def test_el_sucesor_libera_el_terminal_vencido_al_recuperar_pool(self):
        pool, inicio = self.preparar_pool()
        anterior = self.guardar_lote_doble()
        sano = self.proceso_pool(pool)
        with patch('operativo.subprocess', sano):
            self.tick(100)

        with patch('operativo.subprocess', self.proceso_pool(pool, ('reservar', anterior.id))):
            pool.api.now = inicio + 120
            self.ver_ci(anterior, 'rojo', n=2)
            self.tick(220)
            self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
            self.observador.observacion = replace(observacion(2), base='')
            for ahora in (340, 460):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
        self.assertEqual(self.reservas_pool(pool), {anterior.id})

        self.observador.observacion = observacion(2)
        with patch('operativo.subprocess', sano):
            pool.api.now = inicio + 480
            self.tick(580)
            sucesor = self.estado().trenes[-1]
            self.assertNotEqual(sucesor.id, anterior.id)
            self.assertNotIsInstance(sucesor.fase, Terminal)
            self.assertEqual(sucesor.recibos, anterior.recibos)
            self.assertEqual(self.reservas_pool(pool), {sucesor.id})
            self.cli('pausa', '--motivo', 'prueba', ahora=581)
            pool.api.now = inicio + 600
            self.tick(700)
        self.assert_pool(pool, set(), 0)

    def test_pool_viejo_renueva_al_sucesor_aunque_rechace_el_recorte(self):
        pool, inicio = self.preparar_pool()
        anterior = self.guardar_lote_doble()
        with patch('operativo.subprocess', self.proceso_pool_viejo(pool)):
            self.tick(100)
            nuevo, efectos = separar_devueltos(self.estado(), anterior, BASE, {2}, ())
            pool.api.now = inicio + 120
            with patch('operativo.paso', return_value=(nuevo, efectos)):
                self.tick(220)
            sucesor = self.estado().trenes[-1]
            self.assertNotIsInstance(sucesor.fase, Terminal)
            self.assertEqual(self.reservas_pool(pool), {sucesor.id})
            pool.api.now = inicio + 240
            self.tick(340)

        self.assertEqual(self.reservas_pool(pool), {sucesor.id})
        self.assertEqual(self.fallos(), [dict(lote=anterior.id, comando='reservar',
                                              clase='CalledProcessError', codigo=64)])

    def test_pool_viejo_libera_en_la_pausa_por_escritor_externo(self):
        pool, inicio = self.preparar_pool()
        C, arbol, B = CANDIDATO.C, CANDIDATO.arbol, CANDIDATO.B
        tren = self.guardar(Promoviendo(Certificado(C, arbol, 'suite.json', 'verde', B)))
        main_ajeno = 'f' * 40
        with patch('operativo.subprocess', self.proceso_pool_viejo(pool)):
            self.tick(100)
            self.assertEqual(self.reservas_pool(pool), {tren.id})
            pool.api.now = inicio + 120
            self.observador.observacion = observacion(
                main=EscritorExterno(B, C, main_ajeno), tren_id=tren.id, intento_promocion=1)
            self.tick(220)

        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.assertEqual(self.estado().pausa, Pausa('escritor-externo', main=main_ajeno))
        self.assert_pool(pool, set(), 0)
        self.assertEqual(self.fallos(), [dict(lote=tren.id, comando='reservar',
                                              clase='CalledProcessError', codigo=64)])

    def comprobar_cierre_sin_sucesor(self, viejo, pausar):
        pool, inicio = self.preparar_pool()
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        proceso = self.proceso_pool_viejo(pool) if viejo else self.proceso_pool(pool)
        with patch('operativo.subprocess', proceso):
            self.tick(100)
            self.ver_ci(tren, 'rojo')
            pool.api.now = inicio + 120
            self.tick(220)
            self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
            self.observador.observacion = replace(self.observador.observacion, prs=())
            if pausar:
                self.cli('pausa', '--motivo', 'prueba', ahora=221)
            for ahora in (340, 460, 580, 700):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
                if ahora == 460 and not pausar:
                    self.assert_pool(pool, {tren.id}, 75)
                if ahora == 340 and pausar:
                    self.assertEqual(self.reservas_pool(pool), set())

        if pausar:
            self.assertEqual(self.estado().pausa, Pausa('manual', detalle='prueba'))
        self.assert_pool(pool, set(), 0)

    def test_sin_sucesor_pool_viejo(self):
        self.comprobar_cierre_sin_sucesor(viejo=True, pausar=False)

    def test_sin_sucesor_control(self):
        self.comprobar_cierre_sin_sucesor(viejo=False, pausar=False)

    def test_pausa_pool_viejo(self):
        self.comprobar_cierre_sin_sucesor(viejo=True, pausar=True)

    def test_pausa_control(self):
        self.comprobar_cierre_sin_sucesor(viejo=False, pausar=True)

    def relevo_devueltos_con_fallo(self, pool, inicio, comando):
        anterior = self.guardar_lote_doble()
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick()
        self.assertEqual(self.reservas_pool(pool), {anterior.id})

        # Inyecta la salida de D5 en el tick que persiste y aplica el relevo.
        relevo, efectos = separar_devueltos(self.estado(), anterior, BASE, {2}, ())
        sucesor = relevo.trenes[-1]
        lote = sucesor.id if comando == 'reservar' else anterior.id
        pool.api.now = inicio + 120
        with patch('operativo.paso', return_value=(relevo, efectos)), \
                patch('operativo.subprocess', self.proceso_pool(pool, (comando, lote))):
            self.tick(220)
        self.assertEqual(self.estado().trenes, relevo.trenes)
        self.assertEqual(relevo.trenes[0].fase, Terminal('DEVUELTO'))
        self.assertEqual(self.fallos(), [dict(lote=lote, comando=comando,
                                              clase='CalledProcessError', codigo=1)])
        return anterior.id, sucesor.id

    def test_fallo_al_liberar_devuelto_se_reintenta_con_el_sucesor_vivo(self):
        pool, inicio = self.preparar_pool()
        anterior, sucesor = self.relevo_devueltos_con_fallo(pool, inicio, 'liberar-reserva')
        self.assertEqual(self.reservas_pool(pool), {anterior, sucesor})
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(340)
        self.assertEqual(self.reservas_pool(pool), {sucesor})

    def test_pausa_libera_el_sucesor_y_el_devuelto_tras_un_fallo(self):
        pool, inicio = self.preparar_pool()
        anterior, sucesor = self.relevo_devueltos_con_fallo(pool, inicio, 'liberar-reserva')
        self.assertEqual(self.reservas_pool(pool), {anterior, sucesor})
        self.cli('pausa', '--motivo', 'prueba', ahora=221)
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(340)
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='prueba'))
        codigo, _, error = self.comando_pool(pool, *pool.args('ajena-pausa', 'e2e',
                                                             '--prioridad', 'suite'))
        self.assertEqual((self.reservas_pool(pool), codigo), (set(), 0), error)

    def test_fallo_al_reservar_sobrevivientes_reintenta_y_libera_el_devuelto(self):
        pool, inicio = self.preparar_pool()
        anterior, sucesor = self.relevo_devueltos_con_fallo(pool, inicio, 'reservar')
        self.assertEqual(self.reservas_pool(pool), {anterior})
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick(340)
        self.assertEqual(self.reservas_pool(pool), {sucesor})

    def test_rearmar_el_sucesor_no_prolonga_la_reserva_del_devuelto(self):
        pool, inicio = self.preparar_pool()
        anterior, sucesor = self.relevo_devueltos_con_fallo(pool, inicio, 'liberar-reserva')
        self.assertEqual(self.reservas_pool(pool), {anterior, sucesor})
        self.observador.observacion = replace(observacion(2), base='f' * 40)
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', self.proceso_pool(pool, ('liberar-reserva', anterior))):
            self.tick(340)
        self.assertEqual([t.fase for t in self.estado().trenes],
                         [Terminal('DEVUELTO'), Terminal('REARMAR')])
        self.assert_pool(pool, {anterior, sucesor}, 75)
        self.observador.observacion = replace(self.observador.observacion, prs=())
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            for ahora in (460, 580):
                pool.api.now = inicio + ahora - 100
                self.tick(ahora)
        self.assert_pool(pool, {sucesor}, 75)
        pool.api.now = inicio + 550  # t=650, antes del tick de t=700.
        self.assert_pool(pool, set(), 0)

    def test_rearme_tras_dos_fallos_conserva_cada_relevo_hasta_su_vencimiento(self):
        pool, inicio = self.preparar_pool()
        anterior, sucesor = self.relevo_devueltos_con_fallo(pool, inicio, 'reservar')

        self.observador.observacion = replace(observacion(2), base='f' * 40)
        pool.api.now = inicio + 240
        with patch('operativo.subprocess', self.proceso_pool(pool, ('reservar', sucesor))):
            self.tick(340)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal('REARMAR'))
        self.assertEqual(self.reservas_pool(pool), {anterior})

        self.observador.observacion = replace(self.observador.observacion, prs=())
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            pool.api.now = inicio + 360
            self.tick(460)
            self.assert_pool(pool, {anterior, sucesor}, 75)
            pool.api.now = inicio + 420
            self.assert_pool(pool, {sucesor}, 75)
            pool.api.now = inicio + 540
            self.assert_pool(pool, set(), 0)

    def test_absolver_reserva_y_libera_en_el_mismo_tick(self):
        viejo = self.guardar(Corriendo(CANDIDATO, 'suite.json'),
                             Pausa('necesita-juicio', detalle='sin-costo-de-suite-sana')).id
        self.cli('juicio', '--tren', viejo, '--decision', 'absolver', '--motivo', 'prueba')
        self.tick()
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('ABSUELTO'))
        self.assert_llamadas(self.recorte(viejo, 100, 400),
                             self.reserva(estado.trenes[-1].id, 100), self.liberacion(viejo, 100))

    def test_fallo_al_reservar_el_sucesor_conserva_la_reserva_del_relevo(self):
        pool, inicio = self.preparar_pool()
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        self.ver_ci(tren, 'rojo')
        with patch('operativo.subprocess', self.proceso_pool(pool)):
            self.tick()
        self.registro.entregar(entrega(2), 101)
        self.observador.observacion = observacion(2)
        proceso = self.proceso_pool(pool)
        ejecutar_pool = proceso.run

        def rechazar_reserva_larga(argv, **opciones):
            if '--ttl' in argv:
                raise subprocess.CalledProcessError(1, argv)
            ejecutar_pool(argv, **opciones)

        proceso.run = rechazar_reserva_larga
        pool.api.now = inicio + 120
        with patch('operativo.subprocess', proceso):
            self.tick(220)
        nuevo = self.estado().trenes[-1].id
        self.assertNotEqual(nuevo, tren.id)
        self.assert_pool(pool, {tren.id}, 75)
        self.assertEqual(self.fallos(), [dict(lote=nuevo, comando='reservar',
                                              clase='CalledProcessError', codigo=1)])

    def test_plazo_compartido_agotado_omite_el_segundo_proceso(self):
        viejo = self.guardar(Corriendo(CANDIDATO, 'suite.json'),
                             Pausa('necesita-juicio', detalle='sin-costo-de-suite-sana')).id
        self.cli('juicio', '--tren', viejo, '--decision', 'absolver', '--motivo', 'prueba')
        with patch('operativo.time') as reloj, patch('operativo.subprocess.run') as lanzar:
            reloj.time.return_value = 100
            reloj.monotonic.side_effect = (0, 10, 10, 10)
            self.tick()
        nuevo = self.estado().trenes[-1].id
        lanzar.assert_called_once()
        self.assertEqual(lanzar.call_args.args[0][1:], self.recorte(viejo, 100, 400)[0])
        self.assertEqual(self.fallos(), [dict(lote=nuevo, comando='reservar', clase='TimeoutExpired')])
        self.tick(220)
        self.assert_llamadas(self.recorte(viejo, 220, 400),
                             self.reserva(nuevo, 220), self.liberacion(viejo, 220))

    def test_terminal_historico_sin_evento_no_adquiere_una_reserva(self):
        pool, inicio = self.preparar_pool()
        self.guardar(Terminal('DEVUELTO'))
        self.observador.observacion = replace(observacion(), prs=())
        pool.api.now = inicio + 900
        proceso = self.proceso_pool(pool)
        proceso.run = Mock(wraps=proceso.run)
        with patch('operativo.subprocess', proceso):
            self.tick(1000)
        proceso.run.assert_not_called()
        self.assert_pool(pool, set(), 0)

    def test_host_sin_boat_no_llama_al_pool(self):
        lote = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.observador.config['host'] = 'desktop'
        self.tick()
        self.assert_llamadas()
        self.observador.config['host'] = 'boat'
        self.tick(220)
        self.assert_llamadas(self.reserva(lote, 220))

    def test_aborto_incierto_libera_la_reserva_en_cada_tick_pausado(self):
        B, C, arbol = CANDIDATO.B, CANDIDATO.C, CANDIDATO.arbol
        tren = self.guardar(Promoviendo(Certificado(C, arbol, 'suite.json', 'verde', B)),
                            Pausa('infraestructura', detalle='pre-aterrizaje:prueba'))
        self.tick()
        self.cli('juicio', '--tren', tren.id, '--decision', 'abortar', '--motivo', 'prueba')
        self.git.reconciliar = Mock(return_value=Incierto(B, C, 'prueba'))
        self.tick(220)
        self.assertIsNotNone(self.registro.siguiente_decision(REPO))
        # El falso registra el instante del último COMMIT, que sigue siendo 100.
        self.assert_llamadas(self.liberacion(tren.id, 100), self.liberacion(tren.id, 100))

    def test_fallo_al_reservar_registra_evento_y_conserva_la_salida_del_tick(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        config = self.observador.config
        config['host'] = 'desktop'
        esperado = self.tick()
        estado = self.estado()
        config['host'] = 'boat'
        rechazo, lento = self.raiz / 'pool-fallido', self.raiz / 'pool-lento'
        self.escribir_pool(rechazo, "print('detalle privado', file=sys.stderr)\nsys.exit(1)")
        self.escribir_pool(lento, 'time.sleep(2)')
        fallos = []
        for binario, plazo, clase, extra in (
                (rechazo, 10, 'CalledProcessError', {'codigo': 1}),
                (self.raiz / 'pool-ausente', 10, 'FileNotFoundError', {}),
                (lento, 0.25, 'TimeoutExpired', {})):
            with self.subTest(clase=clase):
                config['boat_pool'] = str(binario)
                with patch('operativo.PLAZO_RESERVA_BOAT_S', plazo):
                    self.assertEqual(self.tick(), esperado)
                self.assertEqual(self.estado(), estado)
                fallos.insert(0, dict(lote=tren.id, comando='reservar', clase=clase, **extra))
                self.assertEqual(self.fallos(), fallos)

    def test_fallo_al_liberar_registra_evento_y_conserva_la_pausa(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1), Pausa('manual', detalle='prueba'))
        self.observador.config['host'] = 'desktop'
        esperado = self.tick()
        estado = self.estado()
        self.escribir_pool(self.binario, 'sys.exit(1)')
        self.observador.config['host'] = 'boat'
        self.assertEqual(self.tick(), esperado)
        self.assertEqual(self.estado(), estado)
        self.assertEqual(self.fallos(), [dict(lote=tren.id, comando='liberar-reserva',
                                              clase='CalledProcessError', codigo=1)])
