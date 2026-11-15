"""R1 U16: solicitud vigente y preparación de runner; sólo host remoto."""
from dataclasses import replace
import json
import subprocess
import unittest

import test_activo as activo
from test_u16 import ContextoCI, REPO
from dominio import Certificado, EsperandoCI, Pausa, Terminal, paso
from observador import GitHubGit


def check(conclusion, suite=1, identidad=90, actions=False):
    valor = {'id': identidad, 'name': 'gate de integración', 'head_sha': 'a' * 40,
             'status': 'completed', 'conclusion': conclusion, 'check_suite': {'id': suite}}
    if actions:
        valor.update(app={'slug': 'github-actions'}, details_url=
            'https://github.com/' + REPO + '/actions/runs/10/job/20')
    return valor


def job(head='a' * 40):
    # Forma observada de un runner que falla y fabrica fallos posteriores.
    pasos = [('Set up job', 'success'), ('Set up runner', 'failure'),
             ('Checkout', 'skipped'), ('Instalar dependencias', 'skipped'),
             ('Lint', 'failure'), ('Tipos', 'failure'), ('Complete job', 'success')]
    return {'head_sha': head, 'check_run_url':
            'https://api.github.com/repos/{}/check-runs/90'.format(REPO),
            'steps': [dict(number=n, name=nombre, status='completed', conclusion=conclusion)
                      for n, (nombre, conclusion) in enumerate(pasos, 1)]}


class ContextoR1(activo.Contexto):
    """Fixture durable: un proceso nuevo observa la misma respuesta de GitHub."""
    def __init__(self, ruta):
        super().__init__(ruta)
        original = self.observador.ejecutar
        def transporte(argv, **kwargs):
            evidencia = self.raiz / 'ci-r1.json'
            if argv[0] == 'gh' and evidencia.exists():
                datos = json.loads(evidencia.read_text())
                endpoint = argv[4]
                candidato = '/commits/' + datos['head'] + '/'
                respuesta = None
                if candidato in endpoint:
                    if '/check-runs?' in endpoint:
                        respuesta = [{'check_runs': datos['checks']}]
                    elif '/check-suites?' in endpoint:
                        respuesta = [{'check_suites': datos.get('suites', [])}]
                    elif '/statuses?' in endpoint:
                        respuesta = [datos.get('statuses', [])]
                elif '/actions/jobs/' in endpoint:
                    if datos.get('job_error'):
                        raise subprocess.CalledProcessError(1, argv)
                    respuesta = datos['job']
                if respuesta is not None:
                    self.log('ci-r1-lecturas.log', endpoint)
                    return subprocess.CompletedProcess(argv, 0, json.dumps(respuesta), '')
            return original(argv, **kwargs)
        self.observador.ejecutar = transporte


class CasosCI(ContextoCI):
    def guardar_ci(self, datos):
        (self.raiz / 'ci-r1.json').write_text(json.dumps(datos))

    def anterior(self, conclusion, individual=True):
        antes = self.individual() if individual else self.hasta_fase(EsperandoCI)
        fase, tren = antes.trenes[0].fase, antes.trenes[0]
        self.ci_en(lambda head: 'pending' if head == fase.candidato.C else None)
        self.contexto.tick(fase.desde + 3600)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.contexto = ContextoR1(self.ruta_contexto)
        viejo = check(conclusion)
        viejo['head_sha'] = fase.candidato.C
        datos = {'head': fase.candidato.C, 'checks': [viejo], 'suites': [
            {'id': 1, 'head_sha': fase.candidato.C, 'head_branch': 'tren/' + tren.id}],
            'statuses': [{'context': 'legacy', 'state': conclusion}]}
        self.guardar_ci(datos)
        self.juicio('reanudar')
        return antes, datos, fase.desde + 4000

    def sin_feedback(self):
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertEqual(self.posts('/comments'), [])
        self.assertFalse(any(p['payload']['state'] == 'failure' for p in self.posts('/statuses/')))
        self.assertEqual(self.lineas('trabajos.log'), [])
        self.assertEqual(self.remoto(), self.B)

    def nueva(self, datos, tren, conclusion):
        # El check viejo tiene mayor id: filtrar la solicitud ANTES de agrupar.
        nuevo = check(conclusion, suite=2, identidad=2)
        nuevo['head_sha'] = datos['head']
        datos['checks'].append(nuevo)
        datos['suites'].append({'id': 2, 'head_sha': datos['head'],
                               'head_branch': 'tren/' + tren.id + '-ci-1'})
        self.guardar_ci(datos)


class RegresionesTests(CasosCI):
    def test_rojo_anterior_no_devuelve_y_rojo_vigente_si(self):
        antes, datos, ahora = self.anterior('failure')
        self.contexto.tick(ahora)
        self.contexto.tick(ahora + 1)
        self.contexto.tick(ahora + 59 * 60)
        self.sin_feedback()
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.estado().trenes[0].fase.desde, ahora)
        self.nueva(datos, antes.trenes[0], 'failure')
        self.contexto.tick(ahora + 3599)
        self.verificar_devolucion(antes.trenes[0].fase.candidato)

    def test_verde_anterior_no_lanza_suite_y_verde_vigente_si(self):
        antes, datos, ahora = self.anterior('success', individual=False)
        self.contexto.tick(ahora)
        self.contexto.tick(ahora + 1)
        self.sin_feedback()
        self.nueva(datos, antes.trenes[0], 'success')
        # El status viejo rojo tampoco debe contaminar la nueva solicitud.
        datos['statuses'][0]['state'] = 'failure'
        self.guardar_ci(datos)
        self.contexto.tick(ahora + 2)
        self.assertEqual(len(self.lineas('trabajos.log')), 1)
        self.contexto.tick(ahora + 3)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertIn(':tren/' + antes.trenes[0].id + '-ci-1', self.estado().trenes[0].fase.ci)

    def test_muerte_tras_publicar_conserva_solicitud_y_plazo(self):
        antes, datos, ahora = self.anterior('failure')
        codigo = '''
from test_u16_r1 import ContextoR1
import os
def frontera(nombre, efecto):
    if nombre == 'efecto-ejecutado' and efecto.tipo == 'solicitar-ci':
        os._exit(77)
ContextoR1(sys.argv[3]).tick(int(sys.argv[4]), frontera)
'''
        hijo = self.hijo(codigo, str(ahora))
        salida, error = hijo.communicate(timeout=60)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assertEqual(self.remoto('refs/heads/tren/' + antes.trenes[0].id + '-ci-1'), datos['head'])
        self.contexto = ContextoR1(self.ruta_contexto)
        self.contexto.tick(ahora + 1)
        self.contexto.tick(ahora + 3599)
        self.sin_feedback()
        self.assertEqual(self.estado().trenes[0].fase.solicitud, 1)
        self.assertEqual(self.estado().trenes[0].fase.desde, ahora)
        self.contexto.tick(ahora + 3600)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))

    def test_fallo_runner_espera_sin_devolver_ni_partir(self):
        antes = self.hasta_fase(EsperandoCI)
        fase = antes.trenes[0].fase
        self.contexto = ContextoR1(self.ruta_contexto)
        rojo = check('failure', actions=True)
        rojo['head_sha'] = fase.candidato.C
        self.guardar_ci({'head': fase.candidato.C, 'checks': [rojo], 'job': job(fase.candidato.C)})
        self.contexto.tick(fase.desde + 1)
        self.sin_feedback()
        self.contexto.tick(fase.desde + 3600)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.sin_feedback()
        self.assertEqual(len(self.estado().trenes), 1)
        self.assertEqual(len([p for p in self.lineas('ci-r1-lecturas.log') if '/actions/jobs/' in p]), 2)


class ContratoTests(CasosCI):
    def test_recibo_antiguo_no_autoriza_verde_en_reductor_ni_ejecutor(self):
        from efectos import ejecutar
        from dominio import Efecto, clave
        antes, datos, ahora = self.anterior('success')
        self.contexto.tick(ahora)
        self.nueva(datos, antes.trenes[0], 'success')
        estado = self.estado()
        tren = estado.trenes[0]
        obs = self.contexto.observador.observar(estado, ahora + 1)
        self.assertEqual(obs.ci_candidato, 'verde')
        self.assertEqual(obs.ci_recibo, 'github:' + datos['head'] + ':tren/' + tren.id + '-ci-1')
        vieja = replace(obs, ci_recibo='github:' + datos['head'])
        _, efectos = paso(estado, vieja)
        self.assertFalse(any(e.tipo == 'iniciar-suite' for e in efectos))
        efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
        llegada = ejecutar(efecto, estado, vieja, self.contexto.git, self.contexto.hasito)
        self.assertFalse(llegada.completo)
        self.assertEqual(self.lineas('trabajos.log'), [])

    def test_error_api_jobs_no_se_convierte_en_rojo(self):
        antes = self.individual()
        fase = antes.trenes[0].fase
        self.contexto = ContextoR1(self.ruta_contexto)
        rojo = check('failure', actions=True)
        rojo['head_sha'] = fase.candidato.C
        self.guardar_ci({'head': fase.candidato.C, 'checks': [rojo], 'job_error': True})
        self.contexto.tick(fase.desde + 1)
        self.sin_feedback()
        self.contexto.tick(fase.desde + 3600)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.sin_feedback()


class LecturaActionsTests(unittest.TestCase):
    def test_solicitud_filtra_ref_y_c_antes_de_agrupar_en_todas_las_paginas(self):
        lector = GitHubGit({'checks_esperados': ['gate de integración']}, activo=True)
        viejo, nuevo = check('failure'), check('success', suite=2, identidad=2)
        ajeno = check('failure', suite=2, identidad=100)
        ajeno['head_sha'] = 'b' * 40
        def api(endpoint, paginado=False):
            self.assertTrue(paginado)
            if '/check-runs?' in endpoint:
                self.assertIn('&filter=all', endpoint)
                return [{'check_runs': [viejo, ajeno]}, {'check_runs': [nuevo]}]
            if '/check-suites?' in endpoint:
                return [{'check_suites': [{'id': 1, 'head_sha': 'a' * 40,
                                          'head_branch': 'tren/otra'}]},
                        {'check_suites': [{'id': 2, 'head_sha': 'a' * 40,
                                          'head_branch': 'tren/actual-ci-1'}]}]
            return [[{'context': 'legacy', 'state': 'failure'}]]
        lector.api = api
        self.assertEqual(lector.ci(REPO, 'a' * 40, 'tren/actual-ci-1').estado, 'verde')

    def test_status_solo_no_autoriza_solicitud_ni_con_ci_configurado_vacio(self):
        for estado in ('success', 'failure'):
            with self.subTest(estado=estado):
                lector = GitHubGit({'checks_esperados': []}, activo=True)
                lector.api = lambda endpoint, paginado=False: (
                    [{'check_runs': []}] if '/check-runs?' in endpoint else
                    [{'check_suites': []}] if '/check-suites?' in endpoint else
                    [[{'context': 'legacy', 'state': estado}]])
                self.assertEqual(lector.ci(REPO, 'a' * 40, 'tren/actual-ci-1').estado, 'pendiente')

    def leer(self, checks, datos=None, error=None):
        llamadas = []
        lector = GitHubGit({}, activo=True)
        def api(endpoint, paginado=False):
            llamadas.append(endpoint)
            if '/check-runs?' in endpoint:
                return [{'check_runs': checks}]
            if '/statuses?' in endpoint:
                return [[]]
            self.assertIn('/actions/jobs/20', endpoint)
            if error:
                raise error
            return datos
        lector.api = api
        resultado = lector.ci(REPO, 'a' * 40)
        return resultado, [e for e in llamadas if '/actions/jobs/' in e]

    def test_workflow_fallido_sigue_rojo_con_una_consulta(self):
        datos = job()
        datos['steps'][1]['conclusion'] = 'success'
        datos['steps'][2]['conclusion'] = 'success'
        resultado, llamadas = self.leer([check('failure', actions=True)], datos)
        self.assertEqual(resultado.estado, 'rojo')
        self.assertEqual(resultado.rojos, ('gate de integración',))
        self.assertEqual(len(llamadas), 1)

    def test_fallo_preparacion_y_evidencia_incierta_son_pendientes(self):
        variantes = [job(), {'head_sha': 'a' * 40}, job('b' * 40)]
        sin_pasos = job()
        sin_pasos['steps'] = []
        variantes.append(sin_pasos)
        setup_job = job()
        setup_job['steps'][0]['conclusion'] = 'failure'
        variantes.append(setup_job)
        for datos in variantes:
            with self.subTest(datos=datos):
                resultado, llamadas = self.leer([check('failure', actions=True)], datos)
                self.assertEqual(resultado.estado, 'pendiente')
                self.assertEqual(len(llamadas), 1)
        for error in (OSError(), ValueError(), subprocess.TimeoutExpired('gh', 60)):
            with self.subTest(error=type(error).__name__):
                resultado, llamadas = self.leer([check('failure', actions=True)], error=error)
                self.assertEqual(resultado.estado, 'pendiente')
                self.assertEqual(len(llamadas), 1)

    def test_solo_failure_de_actions_consulta_jobs_y_sombra_no_cambia(self):
        for conclusion in ('success', 'skipped', 'neutral', 'cancelled', 'timed_out'):
            with self.subTest(conclusion=conclusion):
                resultado, llamadas = self.leer([check(conclusion, actions=True)])
                self.assertEqual(llamadas, [])
        resultado, llamadas = self.leer([check('failure')])
        self.assertEqual(resultado.estado, 'rojo')
        self.assertEqual(llamadas, [])
        for activo_flag, sombra in ((False, True), (False, False)):
            lector = GitHubGit({}, activo=activo_flag, sombra=sombra)
            lector.api = lambda endpoint, paginado=False: (
                [{'check_runs': [check('failure', actions=True)]}]
                if '/check-runs?' in endpoint else [[]])
            self.assertEqual(lector.ci(REPO, 'a' * 40).estado, 'pendiente')

    def test_no_asume_que_job_id_sea_el_check_id(self):
        rojo = check('failure', actions=True)
        rojo['details_url'] = 'https://example.invalid/actions/runs/10/job/20'
        resultado, llamadas = self.leer([rojo])
        self.assertEqual(resultado.estado, 'pendiente')
        self.assertEqual(llamadas, [])
        datos = job()
        datos['check_run_url'] = datos['check_run_url'].replace('/90', '/91')
        resultado, llamadas = self.leer([check('failure', actions=True)], datos)
        self.assertEqual(resultado.estado, 'pendiente')
        self.assertEqual(len(llamadas), 1)


if __name__ == '__main__':
    unittest.main()
