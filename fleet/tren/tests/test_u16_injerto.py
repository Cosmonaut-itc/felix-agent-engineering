"""Injerto U16: regresiones sobre 50cb5cda y garantías conservadas de la base."""
from dataclasses import replace
import json
import unittest

import test_u16 as u16
from dominio import (Certificado, Efecto, EsperandoCI, Pausa, Promoviendo,
                     Terminal, datos, desde_datos, paso)
from feedback import EscritorGitHub
from observador import GitHubGit


class RegresionesTests(u16.ContextoCI):
    def test_cancelled_e_infraestructura_no_devuelven_al_dueno(self):
        antes = self.individual()
        c = antes.trenes[0].fase.candidato.C
        for conclusion in ('cancelled', 'action_required', 'startup_failure', 'stale'):
            with self.subTest(conclusion=conclusion):
                self.ci_en(lambda head: conclusion if head == c else None)
                self.contexto.tick(1000)
                self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
                self.assertIsNone(self.estado().entradas[0].terminal)
                self.assertEqual(self.posts('/comments'), [])
                self.assertFalse(any(p['payload']['state'] == 'failure'
                                     for p in self.posts('/statuses/')))
        self.assertEqual(self.lineas('trabajos.log'), [])
        self.assertEqual(self.remoto(), self.B)

    def test_skipped_y_neutral_esperados_son_verde_solo_en_activo(self):
        for conclusion in ('skipped', 'neutral'):
            with self.subTest(conclusion=conclusion):
                config = {'checks_esperados': ['gate']}
                for activo, sombra in ((True, False), (False, False), (False, True)):
                    lector = GitHubGit(config, activo=activo, sombra=sombra)
                    lector.api = lambda endpoint, paginado: (
                        [{'check_runs': [{'id': 1, 'name': 'gate', 'status': 'completed',
                                          'conclusion': conclusion}]}]
                        if '/check-runs?' in endpoint else [[]])
                    self.assertEqual(lector.ci(u16.REPO, 'a' * 40).estado,
                                     'verde' if activo else 'pendiente')

    def test_rojo_presente_decide_aunque_falte_un_esperado(self):
        antes = self.individual()
        c = antes.trenes[0].fase.candidato.C
        self.contexto.config['checks_esperados'] = ['gate de integración', 'ausente']
        self.ci_en(lambda head: 'failure' if head == c else None)
        self.contexto.tick(1000)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
        self.verificar_devolucion(antes.trenes[0].fase.candidato)

    def partir_y_formar_uno(self):
        antes = self.hasta_fase(EsperandoCI)
        c = antes.trenes[0].fase.candidato.C
        self.ci_en(lambda head: 'failure' if head == c else 'success')
        self.contexto.tick(1000)
        if self.estado().trenes[0].fase == Terminal('REARMAR'):
            self.contexto.tick(self.ci_del_lote_rearmado(1020))
        self.assertEqual(self.estado().trenes[-1].fase, Terminal('PARTIDO'))
        self.contexto.tick(1100)
        individual = self.estado()
        self.assertEqual(individual.trenes[-1].recibos, (antes.entradas[0].entrega.recibo,))
        return antes

    def comprobar_reformado(self, antes):
        self.contexto.tick(9000)
        estado = self.estado()
        self.assertEqual(estado.trenes[-1].recibos, (antes.entradas[0].entrega.recibo,))
        self.assertTrue(estado.entradas[0].partida and estado.entradas[0].solo)
        self.assertEqual(estado.entradas[0].admitida, antes.entradas[0].admitida)
        self.assertEqual(desde_datos(datos(estado)), estado)
        self.assertEqual(self.posts('/comments'), [])

    def test_partida_dura_tras_formar_y_abortar_un_individual(self):
        antes = self.partir_y_formar_uno()
        solo = self.siguiente_hasta(lambda e: isinstance(e.trenes[-1].fase, EsperandoCI), 1200)
        c = solo.trenes[-1].fase.candidato.C
        self.ci_en(lambda head: 'pending' if head == c else None)
        self.contexto.tick(solo.trenes[-1].fase.desde + 3600)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.juicio('abortar')
        self.contexto.tick(8000)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal('ABORTADO'))
        self.ci_en(lambda head: 'success')
        self.comprobar_reformado(antes)

    def test_partida_dura_tras_formar_y_rearmar_un_individual(self):
        antes = self.partir_y_formar_uno()
        self.siguiente_hasta(lambda e: isinstance(e.trenes[-1].fase, Promoviendo), 1200)
        # Escritor externo: otra base sin los heads entregados, para que sigan abiertos.
        externo = self.commit({'comun': 'base\n', 'externo': 'otra base\n'}, (self.B,))
        self.comando(self.fuente, 'push', '--force', str(self.origen),
                     externo + ':refs/heads/main')
        self.contexto.tick(7000)
        self.assertEqual(self.estado().trenes[-1].fase, Terminal('REARMAR'))
        self.juicio('reanudar')
        self.contexto.tick(8000)
        self.comprobar_reformado(antes)

    def test_reductor_rechaza_rojo_sin_nombres(self):
        antes = self.individual()
        obs = self.contexto.observador.observar(antes, 1000)
        for nombres in ((), ('',), (' ',), (None,), 'gate', None):
            with self.subTest(nombres=nombres):
                despues, efectos = paso(antes, replace(obs, ci_candidato='rojo', ci_rojos=nombres))
                self.assertEqual(despues.trenes, antes.trenes)
                self.assertFalse(any(e.tipo == 'devolver' for e in efectos))

    def test_feedback_no_publica_rojo_sin_nombres(self):
        antes = self.individual()
        tren, entrada = antes.trenes[0], antes.entradas[0]
        c = tren.fase.candidato
        llamadas = []
        escritor = EscritorGitHub()
        escritor.api = lambda *args, **kwargs: llamadas.append((args, kwargs)) or [[]]
        escritor.comentario = lambda *args: llamadas.append(args) or (
            'https://github.com/example/project/pull/1#issuecomment-1')
        for nombres in ([], [''], [' '], [None], 'gate', None):
            with self.subTest(nombres=nombres):
                payload = json.dumps({'lote': tren.id, 'B': c.B, 'C': c.C,
                    'pr': entrada.entrega.pr, 'head': entrada.entrega.head,
                    'fallos': [], 'ci_rojos': nombres})
                efecto = Efecto('rojo-invalido', 'devolver', (tren.id, entrada.entrega.recibo, payload))
                with self.assertRaisesRegex(ValueError, 'CI rojo'):
                    escritor.ejecutar_feedback(efecto, antes, lambda *args: None)
                self.assertEqual(llamadas, [])


class RelojTests(u16.ContextoCI):
    def test_verde_sin_lanzamiento_no_detiene_el_plazo(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        obs = self.contexto.observador.observar(antes, fase.desde + 1)
        verde, efectos = paso(antes, obs)
        self.assertTrue(any(e.tipo == 'iniciar-suite' for e in efectos))
        self.assertIsInstance(verde.trenes[0].fase, EsperandoCI)
        pendiente = replace(obs, ahora=fase.desde + 3600, ci_candidato='pendiente')
        despues, efectos = paso(verde, pendiente)
        self.assertEqual(despues.pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.assertEqual(efectos, ())

    def test_pausa_manual_no_reinicia_el_reloj(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        self.ci_en(lambda head: 'pending' if head == fase.candidato.C else None)
        self.assertEqual(self.cli('pausa', '--motivo', 'inspección')[0], 0)
        self.contexto.tick(fase.desde + 100)
        self.assertEqual(self.estado().pausa.motivo, 'manual')
        self.contexto.tick(fase.desde + 3600)
        self.assertEqual(self.estado().trenes[0].fase.desde, fase.desde)
        self.assertEqual(self.cli('pausa', '--levantar', '--motivo', 'continuar')[0], 0)
        self.contexto.tick(fase.desde + 3601)
        self.contexto.tick(fase.desde + 3602)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.assertEqual(self.estado().trenes[0].fase.desde, fase.desde)

    def test_recibo_suite_guardado_se_recupera_aunque_ci_vuelva_pendiente(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        self.contexto.tick(fase.desde + 1)
        self.assertEqual(len(self.lineas('trabajos.log')), 1)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.ci_en(lambda head: 'pending' if head == fase.candidato.C else None)
        self.contexto.tick(fase.desde + 3600)
        self.assertIsNone(self.estado().pausa)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual(len(self.lineas('trabajos.log')), 1)


if __name__ == '__main__':
    unittest.main()
