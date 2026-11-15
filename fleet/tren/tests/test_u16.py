"""U16: CI sobre C, partición FIFO, juicio y recuperación; sólo host remoto."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
import unittest

import test_activo as activo
import test_juicio as juicio
from dominio import (Admitido, Armado, Entrada, Entrega, EsperandoCI, Observacion,
                     Pausa, Terminal, datos, desde_datos, paso)
from observador import GitHubGit

REPO = activo.REPO


class ContextoCI(unittest.TestCase):
    def setUp(self):
        activo.ActivoTests.setUp(self)
        # El contrato de config.git exige los objetos del main observado.
        # Este espejo local conoce cada C publicado, incluso tras aterrizar
        # el primer tren; fuente.git sólo contiene los heads originales.
        self.contexto.config['git'] = str(self.origen)
        self.ruta_contexto.write_text(json.dumps(self.contexto.config))

    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo
    posts = juicio.JuicioTests.posts
    cli = juicio.JuicioTests.cli
    juicio = juicio.JuicioTests.juicio

    def ci_en(self, resolver):
        """Cambiar sólo la API de checks, nunca el dominio ni su lectura de CI."""
        original = self.contexto.observador.ejecutar
        def transporte(argv, **kwargs):
            resultado = original(argv, **kwargs)
            if argv[0] == 'gh' and '/check-runs?' in argv[4]:
                head = argv[4].split('/')[4]
                conclusion = resolver(head)
                if conclusion is not None:
                    valor = [{'check_runs': [{'id': 1, 'name': 'gate de integración',
                        'status': 'in_progress' if conclusion == 'pending' else 'completed',
                        'conclusion': None if conclusion == 'pending' else conclusion}]}]
                    resultado.stdout = json.dumps(valor)
            return resultado
        self.contexto.observador.ejecutar = transporte

    def individual(self):
        # Mantener a los otros miembros en custodia, sin admisión aún.
        for pr in (2, 3):
            (self.raiz / ('apto-{}.json'.format(pr))).write_text('{}')
        self.contexto.tick(100)
        self.contexto.tick(3700)
        return self.hasta_fase(EsperandoCI)

    def siguiente_hasta(self, condicion, inicio=1000, limite=50):
        for n in range(limite):
            self.contexto.tick(inicio + n)
            estado = self.estado()
            self.assertIsNone(estado.pausa, datos(estado.pausa))
            if condicion(estado):
                return estado
        self.fail('no convergió: ' + str(datos(estado)))

    def devolver_uno(self):
        antes = self.individual()
        candidato = antes.trenes[0].fase.candidato
        self.ci_en(lambda head: 'failure' if head == candidato.C else None)
        self.contexto.tick(1000)
        return antes, candidato

    def verificar_devolucion(self, candidato):
        final = self.estado()
        self.assertEqual(final.trenes[0].fase, Terminal('DEVUELTO'))
        self.assertEqual(final.entradas[0].terminal, Terminal('DEVUELTO'))
        self.assertIsNone(final.pausa)
        comentarios = self.posts('/comments')
        self.assertEqual(len(comentarios), 1)
        self.assertIn('gate de integraci', comentarios[0]['payload']['body'])
        self.assertIn(candidato.C, comentarios[0]['payload']['body'])
        self.assertIn('CI rojo', comentarios[0]['payload']['body'])
        fallos = [p for p in self.posts('/statuses/') if p['payload']['state'] == 'failure']
        self.assertEqual(len(fallos), 1)
        self.assertTrue(fallos[0]['endpoint'].endswith(self.miembros[0][1]))
        self.assertEqual(self.remoto(), self.B)
        self.assertFalse(any(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log')))
        self.assertEqual(self.lineas('trabajos.log'), [])


class RegresionesTests(ContextoCI):
    def test_tres_rojo_parte_y_los_individuales_salen_en_orden(self):
        antes = self.hasta_fase(EsperandoCI)
        candidato = antes.trenes[0].fase.candidato
        # El lote conjunto es rojo; el segundo PR también lo es cuando viaja solo.
        causal = self.miembros[1][1]
        def conclusion(head):
            if head in dict(self.miembros).values():
                return None
            contiene = self.comando(self.origen, 'merge-base', causal, head) == causal
            return 'failure' if contiene else 'success'
        self.ci_en(conclusion)
        self.contexto.tick(1000)
        partido = self.estado()
        self.assertEqual(partido.trenes[0].fase, Terminal('PARTIDO'))
        self.assertTrue(all(e.tren is None and e.solo for e in partido.entradas))
        self.assertEqual([(e.entrega, e.recibida, e.admitida) for e in partido.entradas],
                         [(e.entrega, e.recibida, e.admitida) for e in antes.entradas])
        self.assertEqual(self.posts('/comments'), [])
        self.assertFalse(any(p['payload']['state'] == 'failure' for p in self.posts('/statuses/')))
        self.assertEqual(self.remoto(), self.B)
        final = self.siguiente_hasta(lambda e: len(e.trenes) == 4 and
                                    all(isinstance(t.fase, Terminal) for t in e.trenes))
        self.assertEqual([t.recibos for t in final.trenes[1:]],
                         [(e.entrega.recibo,) for e in antes.entradas])
        self.assertEqual([t.fase.resultado for t in final.trenes],
                         ['PARTIDO', 'ATERRIZADO', 'DEVUELTO', 'ATERRIZADO'])
        self.assertEqual(len(self.lineas('trabajos.log')), 2)
        self.assertEqual(len(self.posts('/comments')), 1)
        self.assertEqual(sum(p['payload']['state'] == 'failure' for p in self.posts('/statuses/')), 1)
        self.assertEqual(sum(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log')), 2)
        ramas = self.comando(self.origen, 'for-each-ref', '--format=%(refname)', 'refs/heads/tren/').splitlines()
        self.assertEqual(len(ramas), 4)
        for tren in final.trenes[1:]:
            c = self.remoto('refs/heads/tren/' + tren.id)
            self.assertNotEqual(c, candidato.C)
            self.assertTrue(any('/commits/' + c + '/check-runs' in p for p in self.lineas('github.log')))
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])

    def test_uno_rojo_devuelve_una_vez_y_libera_el_carril(self):
        _, candidato = self.devolver_uno()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('DEVUELTO'))
        for n in range(3):
            self.contexto.tick(1100 + n)
        self.verificar_devolucion(candidato)
        # Nueva entrega sensible autorizada forma tren en el tick inmediato.
        pr, head = self.miembros[1]
        (self.raiz / 'apto-2.json').write_text(json.dumps({
            'head': head, 'veredicto': 'APTO', 'hallazgos_abiertos': 0}))
        original = self.contexto.observador.vista
        self.contexto.observador.vista = lambda entrega: replace(original(entrega),
            sensible=True, autorizado=True) if entrega.pr == pr else original(entrega)
        self.contexto.tick(1200)
        self.assertEqual(len(self.estado().trenes), 2)
        self.assertEqual(self.estado().trenes[-1].recibos, (self.estado().entradas[1].entrega.recibo,))

    def test_pendiente_59_60_reanudar_reinicia_y_verde_aterriza(self):
        antes = self.hasta_fase(EsperandoCI)
        candidato = antes.trenes[0].fase.candidato
        self.ci_en(lambda head: 'pending' if head == candidato.C else None)
        # Funciona también sobre main para que ANTES falle por la transición,
        # no por la ausencia del nuevo campo desde.
        desde = getattr(antes.trenes[0].fase, 'desde', 340)
        (self.raiz / 'apto-1.json').write_text('{}')
        self.contexto.tick(desde + 59 * 60)
        self.assertIsNone(self.estado().pausa)
        self.contexto.tick(desde + 60 * 60)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.assertEqual(self.lineas('trabajos.log'), [])
        self.assertEqual(self.posts('/comments'), [])
        self.assertEqual(self.cli('juicio', '--tren', antes.trenes[0].id,
            '--decision', 'absolver', '--motivo', 'no hay prueba')[0], 2)
        (self.raiz / 'apto-1.json').write_text(json.dumps({
            'head': self.miembros[0][1], 'veredicto': 'APTO', 'hallazgos_abiertos': 0}))
        self.juicio('reanudar')
        reanudado = desde + 4000
        self.contexto.tick(reanudado)
        fase = self.estado().trenes[0].fase
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(fase.candidato, candidato)
        self.assertEqual(fase.desde, reanudado)
        self.assertEqual(fase.solicitud, 1)
        self.assertEqual(self.remoto('refs/heads/tren/' + antes.trenes[0].id + '-ci-1'), candidato.C)
        self.contexto.tick(reanudado + 59 * 60)
        self.assertIsNone(self.estado().pausa)
        self.ci_en(lambda head: 'success')
        final = self.siguiente_hasta(lambda e: e.trenes[0].fase == Terminal('ATERRIZADO'),
                                    inicio=reanudado + 3599)
        self.assertEqual(self.remoto(), candidato.C)
        self.assertEqual(final.trenes[0].fase, Terminal('ATERRIZADO'))
        self.assertEqual(len(self.lineas('trabajos.log')), 1)
        self.assertEqual(sum(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log')), 1)


class LecturaTests(unittest.TestCase):
    def leer(self, checks=(), statuses=(), esperados=None, sombra=False):
        observador = GitHubGit({'checks_esperados': esperados} if esperados is not None else {},
                              sombra=sombra, activo=not sombra)
        observador.api = lambda endpoint, paginado: (
            [{'check_runs': list(checks)}] if '/check-runs?' in endpoint else [list(statuses)])
        return observador.ci(REPO, 'a' * 40)

    def check(self, conclusion, nombre='gate', id=1, status='completed'):
        return {'id': id, 'name': nombre, 'status': status, 'conclusion': conclusion}

    def test_tabla_completa_de_conclusiones_y_statuses(self):
        esperados = {'success': 'verde', 'failure': 'rojo', 'timed_out': 'rojo',
            'cancelled': 'pendiente', 'skipped': 'verde', 'neutral': 'verde',
            'action_required': 'pendiente', 'startup_failure': 'pendiente', 'stale': 'pendiente'}
        for conclusion, esperado in esperados.items():
            with self.subTest(conclusion=conclusion):
                resultado = self.leer([self.check(conclusion)])
                self.assertEqual(resultado.estado, esperado)
                self.assertEqual(resultado.rojos, ('gate',) if esperado == 'rojo' else ())
                self.assertEqual(self.leer([self.check(conclusion)], sombra=True).estado,
                                 'verde' if conclusion == 'success' else 'pendiente')
        for status, esperado in [('success', 'verde'), ('failure', 'rojo'),
                                 ('error', 'pendiente'), ('pending', 'pendiente')]:
            with self.subTest(status=status):
                resultado = self.leer(statuses=[{'context': 'status', 'state': status}])
                self.assertEqual(resultado.estado, esperado)
                self.assertEqual(resultado.rojos, ('status',) if esperado == 'rojo' else ())

    def test_ultima_corrida_por_nombre_y_todas_las_paginas(self):
        observador = GitHubGit({}, activo=True)
        observador.api = lambda endpoint, paginado: (
            [{'check_runs': [self.check('success', id=9)]},
             {'check_runs': [self.check('failure', id=2)]}] if '/check-runs?' in endpoint else
            [[{'context': 'legacy', 'state': 'success'}], [{'context': 'legacy', 'state': 'error'}]])
        self.assertEqual(observador.ci(REPO, 'a' * 40).estado, 'verde')
        self.assertEqual(self.leer([self.check('failure'), self.check(None, id=2,
            status='queued')]).estado, 'pendiente')

    def test_faltantes_vacio_desconocidos_y_exclusion_de_feedback(self):
        self.assertEqual(self.leer([self.check('success')], esperados=['otro']).estado, 'pendiente')
        self.assertEqual(self.leer().estado, 'pendiente')
        self.assertEqual(self.leer(esperados=[]).estado, 'verde')
        for conclusion in (None, 'futuro'):
            self.assertEqual(self.leer([self.check(conclusion)]).estado, 'pendiente')
        self.assertEqual(self.leer([self.check('failure', status='in_progress')]).estado, 'pendiente')
        self.assertEqual(self.leer(statuses=[{'context': 'legacy', 'state': 'futuro'}]).estado, 'pendiente')
        self.assertEqual(self.leer([self.check('failure', 'pao-tren/resultado')],
            [{'context': 'pao-tren/resultado', 'state': 'failure'}], esperados=[]).estado, 'verde')
        self.assertEqual(self.leer([self.check('failure')], esperados=['otro']).estado, 'rojo')


class InvariantesTests(ContextoCI):
    def test_retencion_previa_a_partir_conserva_el_orden_fifo(self):
        antes = self.hasta_fase(EsperandoCI)
        candidato = antes.trenes[0].fase.candidato.C
        primero = self.miembros[0][1]
        self.ci_en(lambda head: 'pending' if head in (candidato, primero) else None)
        self.contexto.tick(400)
        self.assertEqual(self.estado().entradas[0].admitida, antes.entradas[0].admitida)
        self.ci_en(lambda head: 'failure' if head == candidato else
                   'pending' if head == primero else None)
        self.contexto.tick(500)
        partido = self.estado()
        self.assertEqual(partido.trenes[0].fase, Terminal('PARTIDO'))
        self.assertEqual([e.admitida for e in partido.entradas],
                         [e.admitida for e in antes.entradas])
        self.ci_en(lambda head: 'success')
        self.contexto.tick(600)
        self.assertEqual(self.estado().trenes[-1].recibos, (antes.entradas[0].entrega.recibo,))

    def test_rojo_de_otro_c_o_tren_no_se_consume(self):
        antes = self.hasta_fase(EsperandoCI)
        obs = self.contexto.observador.observar(antes, 1000)
        for campos in ({'ci_recibo': 'github:' + 'a' * 40}, {'tren_id': 'otro'}):
            observacion = replace(obs, ci_candidato='rojo', ci_rojos=('gate',), **campos)
            despues, efectos = paso(antes, observacion)
            self.assertEqual(despues.trenes, antes.trenes)
            self.assertFalse(efectos)

    def test_marca_partida_sobrevive_readmision_y_no_pasa_a_head_nuevo(self):
        antes = self.hasta_fase(EsperandoCI)
        c = antes.trenes[0].fase.candidato.C
        self.ci_en(lambda head: 'failure' if head == c else None)
        self.contexto.tick(1000)
        partido = self.estado()
        self.assertEqual(partido.trenes[0].fase, Terminal('PARTIDO'))
        # Retención transitoria no pierde marca ni instante FIFO.
        pr, head = self.miembros[0]
        (self.raiz / 'apto-1.json').write_text('{}')
        with self.ledger() as ledger:
            estado = replace(partido, pausa=Pausa('manual', detalle='inspección'))
            ledger.guardar_paso(estado, (), 1100)
        self.contexto.tick(1200)
        retenida = self.estado().entradas[0]
        self.assertTrue(retenida.partida and retenida.solo)
        self.assertEqual(retenida.admitida, partido.entradas[0].admitida)
        nuevo = self.commit({'comun': 'base\n', 'nuevo': 'ticket\n'}, (head,))
        self.push(nuevo, 'refs/pull/1/head')
        self.contexto.config['miembros'][0][1] = nuevo
        (self.raiz / 'apto-nuevo.json').write_text(json.dumps({
            'head': nuevo, 'veredicto': 'APTO', 'hallazgos_abiertos': 0}))
        (self.raiz / 'dirigida-nueva.json').write_text(json.dumps({'head': nuevo, 'resultado': 'limpio'}))
        with self.ledger() as ledger:
            ledger.entregar(Entrega(REPO, pr, nuevo, str(self.raiz / 'apto-nuevo.json'),
                                    str(self.raiz / 'dirigida-nueva.json')), 1300)
        self.contexto.tick(1400)
        nueva = self.estado().entradas[-1]
        self.assertIsInstance(nueva.admision, Admitido)
        self.assertFalse(nueva.partida or nueva.solo)
        with self.ledger() as ledger:
            actual = ledger.estado(REPO)
            ledger.guardar_paso(replace(actual, pausa=None), (), 1500)
        self.contexto.tick(1501)
        actual = self.estado()
        self.assertEqual(actual.trenes[-1].recibos, (actual.entradas[1].entrega.recibo,))
        self.assertTrue(actual.entradas[1].partida)
        self.assertTrue(actual.entradas[2].partida)
        self.assertEqual(desde_datos(datos(actual)), actual)

    def test_abortar_timeout_libera_sin_feedback_rojo(self):
        antes = self.hasta_fase(EsperandoCI)
        c = antes.trenes[0].fase.candidato.C
        self.ci_en(lambda head: 'pending' if head == c else None)
        self.contexto.tick(antes.trenes[0].fase.desde + 3600)
        self.juicio('abortar')
        self.contexto.tick(5000)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assertTrue(all(e.tren is None for e in self.estado().entradas))
        self.assertEqual(self.posts('/comments'), [])
        self.assertEqual(self.remoto(), self.B)

    def test_payload_entrada_historico_no_cambia(self):
        entrada = Entrada(Entrega(REPO, 1, 'a' * 40, 'apto', 'dirigida'), 1)
        self.assertNotIn('partida', datos(entrada))
        self.assertEqual(desde_datos(datos(entrada)), entrada)
        self.assertTrue(desde_datos(datos(replace(entrada, partida=True))).partida)

    def test_seco_con_ci_rojo_no_consume_ni_cambia_operativo(self):
        antes = self.hasta_fase(EsperandoCI)
        c = antes.trenes[0].fase.candidato.C
        self.ci_en(lambda head: 'failure' if head == c else None)
        args = activo.cli.parser().parse_args(['tick', '--repo', REPO, '--seco'])
        io = [self.lineas(n) for n in ('pushes.log', 'posts.log', 'trabajos.log')]
        for modo, evento in [('activo', 'sombra'), ('sombra', 'sombra-doble-camino')]:
            self.inscritos.write_text(REPO + ' ' + modo + '\n')
            self.assertEqual(activo.cli.ejecutar(args, self.contexto.observador, 1000)[0], 0)
            self.assertEqual(self.estado(), antes)
            with self.ledger() as ledger:
                fila = ledger.db.execute('SELECT tipo FROM events ORDER BY seq DESC LIMIT 1').fetchone()
                self.assertEqual(fila[0], evento)
        self.assertEqual(io, [self.lineas(n) for n in ('pushes.log', 'posts.log', 'trabajos.log')])


class RecuperacionTests(ContextoCI):
    def morir(self, punto, individual):
        antes = self.individual() if individual else self.hasta_fase(EsperandoCI)
        candidato = antes.trenes[0].fase.candidato
        codigo = '''
from test_u16 import ContextoCI
import os
c = ContextoCI()
c.contexto = __import__('test_activo').Contexto(sys.argv[3])
c.ci_en(lambda head: 'failure' if head == sys.argv[4] else None)
def frontera(nombre, efecto):
    if nombre == sys.argv[5] and (efecto is None or efecto.tipo == 'devolver'):
        os._exit(77)
c.contexto.tick(1000, frontera)
'''
        hijo = self.hijo(codigo, candidato.C, punto)
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        guardado = self.estado()
        self.assertIsInstance(guardado.trenes[0].fase, Terminal)
        self.assertEqual(guardado.trenes[0].fase.resultado, 'DEVUELTO' if individual else 'PARTIDO')
        self.contexto.tick(1100)
        self.contexto.tick(1200)
        if individual:
            self.verificar_devolucion(candidato)
        else:
            self.assertEqual(len(self.estado().entradas), 3)
            self.assertEqual(self.estado().trenes[-1].recibos, (antes.entradas[0].entrega.recibo,))
            self.assertTrue(all(e.tren is None and e.partida for e in self.estado().entradas[1:]))
            self.assertEqual(self.posts('/comments'), [])
            self.assertFalse(any(p['payload']['state'] == 'failure' for p in self.posts('/statuses/')))

    def test_muerte_tras_guardar_particion(self):
        self.morir('paso-guardado', False)

    def test_muerte_tras_guardar_devolucion(self):
        self.morir('paso-guardado', True)

    def test_muerte_tras_comentario(self):
        self.morir('comentario-publicado', True)

    def test_muerte_tras_status(self):
        self.morir('antes-registrar', True)


class SombraTests(unittest.TestCase):
    """Eventos de sombra con heads rojo, skipped y verde; `remoto-u16.sh` los
    compara byte a byte con los de 85138706."""
    comando = juicio.JuicioTests.comando
    commit = juicio.JuicioTests.commit
    push = juicio.JuicioTests.push
    remoto = juicio.JuicioTests.remoto
    ledger = juicio.JuicioTests.ledger

    def setUp(self):
        # Fechas fijas: mismos SHA, recibos e ids de tren en ambos árboles.
        fechas = patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                         "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        fechas.start()
        self.addCleanup(fechas.stop)
        juicio.JuicioTests.setUp(self)

    def test_eventos_de_sombra_no_cambian(self):
        pr1, pr2 = self.miembros[0][1], self.miembros[1][1]
        posts = self.raiz / "posts.log"
        posts.write_text(json.dumps({"endpoint": "repos/{}/statuses/{}".format(REPO, pr1),
                                     "payload": {"context": "otro-check", "state": "failure"}}) + "\n")
        original = self.contexto.gh

        def gh(argv, **kwargs):
            if argv[:2] == ["gh", "api"] and argv[4].startswith(
                    "repos/{}/commits/{}/check-runs?".format(REPO, pr2)):
                valor = [{"check_runs": [{'id': 1, 'name': 'check', 'status': 'completed', 'conclusion': 'success'}, {"id": 2, "name": "opcional",
                                                 "status": "completed", "conclusion": "skipped"}]}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(valor), "")
            return original(argv, **kwargs)

        args = activo.cli.parser().parse_args(["tick", "--repo", REPO, "--seco"])
        for inscripcion, doble in (("sombra", True), ("activo", False)):
            self.inscritos.write_text(REPO + " " + inscripcion + "\n")
            observador = GitHubGit(self.contexto.config, ejecutar=gh, sombra=doble)
            for ahora in (100, 3700, 7300):
                self.assertEqual(activo.cli.ejecutar(args, observador=observador, ahora=ahora)[0], 0)
        with self.ledger() as ledger:
            eventos = [(r["tipo"], r["instante"], json.loads(r["payload"])) for r in ledger.db.execute(
                "SELECT tipo, instante, payload FROM events WHERE tipo IN "
                "('sombra', 'sombra-doble-camino', 'admision-entrega') ORDER BY seq")]
        self.assertEqual(sum(t in ("sombra", "sombra-doble-camino") for t, _, _ in eventos), 6)
        for tipo, _, plan in eventos:
            if tipo == "admision-entrega":
                continue
            admisiones = [e["admision"] for e in plan["estado"]["entradas"]]
            # Rojo y skipped en el head siguen siendo «no verde» para la sombra.
            self.assertEqual([a["_tipo"] for a in admisiones[:2]], ["Retenido", "Retenido"])
            self.assertEqual({a["motivo"] for a in admisiones[:2]}, {"ci-no-verde"})
            self.assertEqual(admisiones[2]["_tipo"], "Admitido")
        texto = json.dumps(eventos, sort_keys=True).replace(str(self.raiz), "<raiz>")
        destino = os.environ.get("PAO_TREN_U16_SOMBRA")
        if destino:
            Path(destino).write_text(texto)


if __name__ == '__main__':
    unittest.main()
