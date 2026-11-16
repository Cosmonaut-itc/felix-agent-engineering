"""U18 R1: barrera durable y aborto tras CAS incierto, CLI y git reales."""
from test_u18_injerto import ContextoInjerto
from dominio import Incierto, Promoviendo, Terminal


class ContextoR1(ContextoInjerto):
    def pausar_con_cancelacion_fallida(self):
        self.preparar()
        (self.raiz / 'cancelacion-falla').write_text('fallar siempre')
        self.modo('pausar')
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.diario()[0][1], 1)

    def pushes_main(self):
        return sum(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log'))

    def obligacion_visible(self):
        salida = self.cli('estado')[1]
        pendientes = salida.get('cancelaciones_pre_aterrizaje', [])
        self.assertEqual(len(pendientes), 1)
        registro, pendiente = self.diario()[0]
        self.assertEqual(pendiente, 1)
        self.assertTrue(registro.get('cancelacion_requerida'))
        self.assertEqual(pendientes, [dict(id=registro['solicitud']['id'],
            tren=registro['solicitud']['tren'], B=registro['solicitud']['B'],
            C=registro['solicitud']['C'], ultimo_error='cancelar-error')])

    def arreglar_cancelacion(self):
        (self.raiz / 'cancelacion-falla').rename(self.raiz / 'cancelacion-recuperada')

    def push_con_lectura_incierta(self):
        self.preparar()
        self.modo('seguir')
        original = self.contexto.git.reconciliar
        self.incierto = True
        def reconciliar(B, C, arbol, heads):
            if self.incierto and self.remoto() != self.B:
                return Incierto(B, C, 'lectura-remota-fallida')
            return original(B, C, arbol, heads)
        self.contexto.git.reconciliar = reconciliar
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.pushes_main(), 1)
        self.assertNotEqual(self.remoto(), self.B)
        self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)

    def comprobar_exito_recuperado(self):
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ATERRIZADO'))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.pushes_main(), 1)
        self.assertFalse(self.llamadas('cancelar'))
        self.assertEqual(self.diario()[0][1], 0)
        self.assertEqual([p['payload']['state'] for p in self.posts('/statuses/')],
                         ['pending'] * 3 + ['success'] * 3)
        with self.ledger() as ledger:
            self.assertFalse(ledger.outbox(self.estado().repo))


class RegresionesTests(ContextoR1):
    def test_abortar_y_reanudar_dejan_armar_ci_y_suite_sin_push_hasta_cancelar(self):
        self.pausar_con_cancelacion_fallida()
        self.juicio('abortar')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        # Reproduce también el reanudar de la pausa artificial de la base.
        # Con la barrera durable, el aborto ya deja el carril sin esa pausa.
        if self.estado().pausa:
            self.juicio('reanudar')
            self.tick()
        else:
            self.assertEqual(self.cli('juicio', '--decision', 'reanudar',
                                     '--motivo', 'continuar')[0], 2)
        self.modo('seguir')
        self.hora_tick += 3600
        self.hasta(lambda: len(self.estado().trenes) == 2 and
                   isinstance(self.estado().trenes[-1].fase, Promoviendo))
        self.assertEqual(len(self.lineas('trabajos.log')), 2)
        verificaciones = len(self.llamadas('verificar'))
        for _ in range(3):
            cancelaciones = len(self.llamadas('cancelar'))
            self.tick()
            self.assertEqual(len(self.llamadas('cancelar')), cancelaciones + 1)
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(self.pushes_main(), 0)
            self.assertEqual(self.remoto(), self.B)
            self.assertEqual(len(self.llamadas('verificar')), verificaciones)
            self.obligacion_visible()
        # El feedback del nuevo tren sale aunque el push permanezca retenido.
        self.assertEqual([p['payload']['state'] for p in self.posts('/statuses/')],
                         ['pending'] * 6)
        self.arreglar_cancelacion()
        self.hasta(lambda: self.estado().trenes[-1].fase == Terminal('ATERRIZADO'))
        self.assertEqual(self.pushes_main(), 1)
        self.assertEqual(self.cli('estado')[1]['cancelaciones_pre_aterrizaje'], [])
        self.assertTrue(all(pendiente == 0 for _, pendiente in self.diario()))

    def test_reanudar_mismo_tren_no_borra_cancelacion_pendiente(self):
        self.pausar_con_cancelacion_fallida()
        verificaciones = len(self.llamadas('verificar'))
        self.juicio('reanudar')
        self.modo('seguir')
        for _ in range(3):
            self.tick()
            self.assertEqual(self.pushes_main(), 0)
            self.assertEqual(self.remoto(), self.B)
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(len(self.llamadas('verificar')), verificaciones)
            self.obligacion_visible()
        self.assertEqual(self.cli('estado', '--juicio')[1]['juicio']['decisiones'][0]
                         ['estado'], 'aplicada')
        self.arreglar_cancelacion()
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(self.pushes_main(), 1)

    def test_abortar_tras_recuperar_lectura_del_cas_conserva_exito(self):
        self.push_con_lectura_incierta()
        self.incierto = False
        self.tick()
        self.assertEqual(self.diario()[0][1], 0)
        self.juicio('abortar')
        self.tick()
        self.tick()
        self.comprobar_exito_recuperado()


class LimitesTests(ContextoR1):
    def test_aborto_incierto_espera_acreditacion_y_recupera_sin_otra_decision(self):
        self.push_con_lectura_incierta()
        self.juicio('abortar')
        for _ in range(2):
            self.tick()
            self.assertIsInstance(self.estado().trenes[0].fase, Promoviendo)
            self.assertEqual(self.cli('estado', '--juicio')[1]['juicio']['decisiones'][0]
                             ['estado'], 'pendiente')
            self.assertEqual(self.diario()[0][1], 1)
            self.assertFalse(self.llamadas('cancelar'))
            self.assertEqual(self.pushes_main(), 1)
        self.incierto = False
        self.tick()
        self.comprobar_exito_recuperado()
        self.assertEqual(self.cli('estado', '--juicio')[1]['juicio']['decisiones'][0]
                         ['estado'], 'rechazada')

    def test_abortar_no_cancela_c_que_ya_contiene_un_main_posterior(self):
        self.push_con_lectura_incierta()
        C = self.remoto()
        self.comando(self.fuente, 'fetch', str(self.origen), C)
        posterior = self.commit({'comun': 'base\n', 'posterior': 'cambio\n'}, (C,))
        self.push(posterior, 'refs/heads/main')
        self.incierto = False
        self.juicio('abortar')
        self.tick()
        # El push adicional es del escritor externo del fixture.
        self.assertEqual(self.remoto(), posterior)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ATERRIZADO'))
        self.assertFalse(self.llamadas('cancelar'))
        self.assertEqual(self.diario()[0][1], 0)
        self.assertEqual(len([p for p in self.posts('/statuses/')
                              if p['payload']['state'] == 'success']), 3)
