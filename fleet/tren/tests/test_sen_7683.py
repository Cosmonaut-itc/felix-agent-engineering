"""Reserva de Boat con tick, ledger y un ejecutable falso, sólo en el host de pruebas."""
from dataclasses import replace
import json
import os
import shlex
import sys
from unittest.mock import Mock, patch

from test_hasito import GitFalso
from test_tren import Aislado, BASE, HEAD, REPO, cli, entrega, observacion
from adaptadores import seleccionar
from adaptadores.jale import Jale
from dominio import (Admitido, Aterrizado, Candidato, Certificado, Corriendo, Entrada, EsperandoCI,
                     Estado, Incierto, Pausa, Promoviendo, Terminal, Tren, datos, recibo_ci)
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
        self.escritor = Mock()
        self.escritor.ejecutar_feedback.return_value = {}

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

    def guardar(self, fase, pausa=None):
        tren = Tren('lote-prueba', (self.entrega.recibo,), fase)
        entrada = Entrada(self.entrega, 1, Admitido(), 1, tren=tren.id)
        self.registro.guardar_paso(Estado(REPO, (entrada,), (tren,), pausa), (), 1)
        return tren

    def estado(self):
        return self.registro.estado(REPO, operativo=True)

    def cli(self, comando, *argv, **kwargs):
        args = cli.parser().parse_args([comando, '--repo', REPO, *argv])
        salida = cli.ejecutar(args, **kwargs)
        self.assertEqual(salida[0], 0, salida)
        return salida

    def tick(self, ahora=100):
        adaptador = seleccionar(self.observador.config, self.git)
        return self.cli('tick', ahora=ahora, observador=self.observador, git=self.git,
                        adaptador=adaptador, escritor=self.escritor)

    def ver_ci(self, tren, color):
        self.observador.observacion = observacion(
            ci_candidato=color, tren_id=tren.id, ci_recibo=recibo_ci(tren),
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
    def reserva(lote, ahora, clases='contract,e2e'):
        return (['reservar', '--repo', REPO, '--lote', lote, '--clases', clases,
                 '--ttl', '1800'], ahora)

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

    def test_aterrizado_libera_solo_en_el_tick_terminal(self):
        B, C, arbol = CANDIDATO.B, CANDIDATO.C, CANDIDATO.arbol
        tren = self.guardar(Promoviendo(Certificado(C, arbol, 'suite.json', 'verde', B)))
        self.observador.observacion = observacion(
            main=Aterrizado(B, C, arbol), tren_id=tren.id, intento_promocion=1)
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ATERRIZADO'))
        self.assert_ultimo_estado_guardado()
        self.tick(220)
        self.assert_llamadas(self.liberacion(tren.id, 100))

    def test_devuelto_por_ci_rojo_libera_y_no_reserva(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1))
        self.ver_ci(tren, 'rojo')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
        self.assert_llamadas(self.liberacion(tren.id, 100))
        self.assert_ultimo_estado_guardado()

    def test_abortado_libera_el_lote(self):
        tren = self.guardar(EsperandoCI(CANDIDATO, 1),
                            Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.cli('juicio', '--tren', tren.id, '--decision', 'abortar', '--motivo', 'prueba')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assert_llamadas(self.liberacion(tren.id, 100))
        self.assert_ultimo_estado_guardado()

    def test_pausa_manual_libera_y_levantar_vuelve_a_reservar(self):
        lote = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.tick()
        self.cli('pausa', '--motivo', 'prueba')
        self.tick(220)
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='prueba'))
        self.assert_ultimo_estado_guardado()
        self.tick(340)
        self.cli('pausa', '--levantar', '--motivo', 'prueba')
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

    def test_rearmar_libera_el_lote_viejo_y_reserva_el_nuevo(self):
        viejo = self.guardar(EsperandoCI(CANDIDATO, 1)).id
        self.tick()
        self.observador.observacion = replace(observacion(), base='f' * 40)
        self.tick(220)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('REARMAR'))
        self.tick(340)
        nuevo = self.estado().trenes[-1].id
        self.assertNotEqual(nuevo, viejo)
        self.assert_llamadas(self.reserva(viejo, 100), self.liberacion(viejo, 220),
                             self.reserva(nuevo, 340))

    def test_absolver_libera_y_reserva_en_el_mismo_tick(self):
        viejo = self.guardar(Corriendo(CANDIDATO, 'suite.json'),
                             Pausa('necesita-juicio', detalle='sin-costo-de-suite-sana')).id
        self.cli('juicio', '--tren', viejo, '--decision', 'absolver', '--motivo', 'prueba')
        self.tick()
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('ABSUELTO'))
        self.assert_llamadas(self.liberacion(viejo, 100), self.reserva(estado.trenes[-1].id, 100))

    def test_plazo_compartido_agotado_omite_el_segundo_proceso(self):
        viejo = self.guardar(Corriendo(CANDIDATO, 'suite.json'),
                             Pausa('necesita-juicio', detalle='sin-costo-de-suite-sana')).id
        self.cli('juicio', '--tren', viejo, '--decision', 'absolver', '--motivo', 'prueba')
        with patch('operativo.time') as reloj, patch('operativo.subprocess.run') as lanzar:
            reloj.monotonic.side_effect = (0, 1, 10)
            self.tick()
        nuevo = self.estado().trenes[-1].id
        lanzar.assert_called_once()
        self.assertEqual(lanzar.call_args.args[0][1:], self.liberacion(viejo, 100)[0])
        self.assertEqual(self.fallos(), [dict(lote=nuevo, comando='reservar', clase='TimeoutExpired')])

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
