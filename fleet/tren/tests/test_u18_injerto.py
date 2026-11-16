"""U18 injerto: composición del CLI, diario y relojes controlados. Sólo remoto."""
import json
from unittest.mock import patch

import test_activo as activo
from test_u18 import AHORA, ContextoU18, REPO, utc
from dominio import Admitido, Terminal
from observador import GitHubGit


class ContextoInjerto(ContextoU18):
    def setUp(self):
        super().setUp()
        self.hora_tick = 5000
        self.privada = self.raiz / 'config-privada.json'

    def tick(self, frontera=None):
        self.privada.write_text(json.dumps({'repos': {REPO: self.config}}))
        args = activo.cli.parser().parse_args(['tick', '--repo', REPO,
                                               '--config', str(self.privada)])
        def crear(config, **kwargs):
            # Configuración y observador productivos; sólo el transporte es falso.
            return GitHubGit(config, ejecutar=self.contexto.gh, **kwargs)
        with patch.object(activo.cli, 'GitHubGit', side_effect=crear), \
                patch('time.time', side_effect=self.reloj):
            codigo, salida = activo.cli.ejecutar(args, None, self.hora_tick,
                self.contexto.git, self.contexto.hasito, frontera, self.contexto.escritor)
        self.assertEqual(codigo, 0, salida)
        self.sin_rojo()
        return salida

    def diario(self):
        with self.ledger() as ledger:
            return [(json.loads(r[0]), r[1]) for r in ledger.db.execute(
                'SELECT payload, pendiente FROM pre_aterrizajes ORDER BY rowid')]

    def ventana_real(self):
        self.config['heads_autorizados'] = [{'head': self.miembros[0][1], 'ventana':
            {'inicio': utc(AHORA - 100), 'fin': utc(AHORA + 10000)}}]


class RegresionesTests(ContextoInjerto):
    def test_abortar_se_consume_con_cancelacion_persistentemente_fallida(self):
        self.preparar()
        (self.raiz / 'cancelacion-falla').write_text('fallar siempre')
        self.modo('pausar')
        self.hasta(lambda: self.estado().pausa is not None)
        anterior = self.diario()[0][0]
        self.juicio('abortar')
        # La obligación conserva el comando que produjo el efecto.
        self.config.pop('pre_aterrizaje')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        decisiones = self.cli('estado', '--juicio')[1]['juicio']['decisiones']
        self.assertEqual(decisiones[0]['estado'], 'aplicada')
        for _ in range(2):
            self.tick()
            self.assertEqual(len(self.estado().trenes), 1)
            self.assertEqual(self.diario(), [(anterior, 1)])
        self.assertEqual(self.remoto(), self.B)
        self.assertTrue(self.llamadas('cancelar'))

    def test_feedback_anterior_sale_aunque_la_limpieza_siga_pendiente(self):
        self.ventana_real()
        original = self.contexto.escritor.ejecutar_feedback
        def retener(efecto, estado, frontera):
            if efecto.tipo == 'avisar-aterrizaje':
                raise OSError('transporte temporalmente caído')
            return original(efecto, estado, frontera)
        self.contexto.escritor.ejecutar_feedback = retener
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes and
                   self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        primero = self.estado().trenes[0]
        self.assertEqual(len(primero.recibos), 1)
        self.hora_tick += 3600
        (self.raiz / 'cancelacion-falla').write_text('fallar siempre')
        self.modo('pausar')
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(len(self.estado().trenes), 2)
        self.contexto.escritor.ejecutar_feedback = original
        self.assertFalse([p for p in self.posts('/statuses/')
                          if p['payload']['state'] == 'success'])
        self.tick()
        exitos = [p for p in self.posts('/statuses/') if p['payload']['state'] == 'success']
        self.assertEqual(len(exitos), 1)
        self.assertTrue(exitos[0]['endpoint'].endswith(self.miembros[0][1]))
        self.assertIsNotNone(self.estado().pausa)
        self.assertEqual(self.diario()[-1][1], 1)
        with self.ledger() as ledger:
            pendientes = [json.loads(f['payload']) for f in ledger.outbox(REPO)]
        self.assertFalse([e for e in pendientes if e['tipo'] == 'avisar-aterrizaje'])

    def test_una_cancelacion_por_id_y_tick_aunque_agote_su_plazo(self):
        self.config['pre_aterrizaje']['plazo_s'] = 60
        self.preparar()
        reloj = [0.0]
        llamadas = []
        def transporte(config, solicitud, accion):
            if accion == 'verificar':
                return 'pausar'
            llamadas.append((solicitud['id'], config['plazo_s']))
            reloj[0] += config['plazo_s']
            return 'timeout'
        with patch('preaterrizaje.invocar', side_effect=transporte), \
                patch('time.monotonic', side_effect=lambda: reloj[0]):
            for _ in range(3):
                inicio, cantidad = reloj[0], len(llamadas)
                self.tick()
                self.assertEqual(len(llamadas) - cantidad, 1)
                self.assertLessEqual(reloj[0] - inicio, 60)
                self.assertEqual(self.diario()[0][1], 1)
        self.assertEqual(len({identidad for identidad, _ in llamadas}), 1)
        self.assertEqual(self.remoto(), self.B)

    def test_espera_total_durable_no_se_renueva_entre_ticks(self):
        self.preparar()
        self.tick()
        identidad = self.llamadas('verificar')[0]['id']
        for avance in (60, 1739):
            self.ahora += avance
            self.tick()
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(self.llamadas('verificar')[-1]['id'], identidad)
        verificaciones = len(self.llamadas('verificar'))
        self.ahora += 1
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.estado().pausa.motivo, 'infraestructura')
        self.assertEqual(self.estado().pausa.detalle, 'pre-aterrizaje:gancho-espera-vencida')
        self.assertEqual(len(self.llamadas('verificar')), verificaciones)
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(self.diario()[0][1], 0)
        expediente = self.cli('estado', '--juicio')[1]['juicio']
        self.assertEqual({a['decision'] for a in expediente['admitidas']}, {'reanudar', 'abortar'})
        self.assertEqual(self.remoto(), self.B)
        self.juicio('reanudar')
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(self.llamadas('verificar')[-1]['id'], identidad)
        self.assertEqual(self.diario()[0][0]['vence_espera'], AHORA + 3600)

    def test_cli_carga_contrato_y_ventana_viaja_sola_sin_retener_otros(self):
        self.ventana_real()
        self.config['pre_aterrizaje']['espera_max_s'] = 240
        self.modo('seguir')
        self.tick()
        self.assertEqual(len(self.estado().trenes[0].recibos), 1)
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in self.estado().entradas))
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.llamadas('verificar')[0]['prs'],
                         [{'pr': 1, 'head': self.miembros[0][1]}])
        otros = self.estado().entradas[1:]
        self.assertTrue(all(isinstance(e.admision, Admitido) and e.tren is None for e in otros))
        self.assertEqual([e.admitida for e in otros], [5000, 5000])
        # Se respeta la espera histórica de un lote de dos; no se retiene su CI.
        self.hora_tick += 3600
        self.hasta(lambda: len(self.estado().trenes) == 2 and
                   self.estado().trenes[-1].fase == Terminal('ATERRIZADO'))
        self.assertEqual(self.llamadas('verificar')[-1]['prs'],
                         [{'pr': p, 'head': h} for p, h in self.miembros[1:]])
        self.assertEqual(len(self.lineas('trabajos.log')), 2)
        self.assertIsNone(self.estado().pausa)

    def test_reloj_se_relee_despues_de_la_ultima_frontera_local(self):
        self.ventana_real()
        self.preparar()
        self.modo('seguir')
        def frontera(nombre, efecto):
            if nombre == 'pre-aterrizaje-verificado':
                self.ahora = AHORA + 10000
        self.tick(frontera)
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.estado().pausa.detalle, 'pre-aterrizaje:ventana-vencida')


class LimitesTests(ContextoInjerto):
    def test_plazo_configurado_vence_durante_un_seguir(self):
        self.config['pre_aterrizaje']['espera_max_s'] = 100
        self.preparar()
        self.modo('vencer')
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.estado().pausa.detalle, 'pre-aterrizaje:gancho-espera-vencida')
        self.assertEqual(self.diario()[0][0]['vence_espera'], AHORA + 100)
        self.assertEqual(self.diario()[0][1], 0)
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(self.remoto(), self.B)

    def test_plazo_total_rechaza_valores_ambiguos(self):
        from preaterrizaje import gancho
        for espera in (0, -1, True, '100', float('inf'), float('nan')):
            with self.subTest(espera=espera), self.assertRaises(ValueError):
                gancho({'pre_aterrizaje': dict(self.config['pre_aterrizaje'], espera_max_s=espera)})

    def test_presupuesto_compartido_por_todos_los_ids_y_acciones_del_tick(self):
        self.config['pre_aterrizaje']['plazo_s'] = 45
        self.preparar()
        self.tick()
        registro = self.diario()[0][0]
        registro['solicitud']['id'] += '-otro'
        registro['solicitud']['tren'] = 'otro-tren-cerrado'
        with self.ledger() as ledger, ledger.transaccion():
            ledger.db.execute('INSERT INTO pre_aterrizajes VALUES (?, ?, ?, 1)',
                (registro['solicitud']['id'], REPO, json.dumps(registro)))
        self.cli('pausa', '--motivo', 'pausa de prueba')
        reloj, llamadas = [0.0], []
        def transporte(config, solicitud, accion):
            llamadas.append((solicitud['id'], accion, config['plazo_s']))
            reloj[0] += config['plazo_s']
            return 'timeout'
        with patch('preaterrizaje.invocar', side_effect=transporte), \
                patch('time.monotonic', side_effect=lambda: reloj[0]):
            self.tick()
        self.assertEqual([plazo for _, _, plazo in llamadas], [45, 15])
        self.assertEqual(len({i for i, _, _ in llamadas}), 2)
        self.assertTrue(all(accion == 'cancelar' for _, accion, _ in llamadas))
        self.assertEqual([pendiente for _, pendiente in self.diario()], [1, 1])

    def test_verificar_y_cancelar_comparten_el_mismo_presupuesto(self):
        self.config['pre_aterrizaje']['plazo_s'] = 45
        self.preparar()
        reloj, llamadas = [0.0], []
        def transporte(config, solicitud, accion):
            llamadas.append((accion, config['plazo_s']))
            reloj[0] += config['plazo_s']
            return 'pausar' if accion == 'verificar' else 'timeout'
        with patch('preaterrizaje.invocar', side_effect=transporte), \
                patch('time.monotonic', side_effect=lambda: reloj[0]):
            self.tick()
        self.assertEqual(llamadas, [('verificar', 45), ('cancelar', 15)])
        self.assertEqual(self.diario()[0][1], 1)
        self.assertEqual(self.remoto(), self.B)

    def test_remoto_puede_aceptar_push_despues_del_fin_de_ventana(self):
        self.ventana_real()
        self.preparar()
        self.modo('seguir')
        transporte = self.contexto.git.ejecutar
        horas = []
        def demorar(argv, **kwargs):
            if 'push' in argv and argv[-1].endswith(':refs/heads/main'):
                horas.append(self.reloj())
                self.ahora = AHORA + 10000
            return transporte(argv, **kwargs)
        self.contexto.git.ejecutar = demorar
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(horas, [AHORA])
        self.assertEqual(self.reloj(), AHORA + 10000)
        self.assertNotEqual(self.remoto(), self.B)
        self.assertFalse(self.llamadas('cancelar'))
