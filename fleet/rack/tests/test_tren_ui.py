"""La sección Tren con el JS de producción y datos validados por el rack."""
import safety
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_tren as contract

BASE, NOW = contract.BASE, contract.NOW
REPO, SHADOW = 'Example/Jale', 'Example/Hasito'
HOSTILE = '<img src=x onerror=alert(1)>'
LONG_UNIT = 'apps/e2e/' + 'comprobantes-de-la-quincena-con-complemento-' * 4 + 'redondeo.test.ts'
COMMENT = 'https://github.com/Example/Jale/pull/905#issuecomment-123'


def documents():
    red = contract.fixture()
    train, shadow = red['repos']
    train.update(repo=REPO, pausa=None)
    shadow.update(repo=SHADOW)
    batch = train['lote']
    batch['suite'][0].update(estado='terminada', resultado='rojo', fallas=2, fin=NOW-26*60)
    batch['suite'][1].update(estado='terminada', resultado='verde', fallas=0, fin=NOW-15*60)
    train['recientes'][0]['miembros'][0]['devolucion']['comentario_url'] = COMMENT
    shadow['recientes'] = [
        dict(id='edd987650001', resultado='SIMULADO', cierre=NOW-2000, fin=NOW-800,
             miembros=[finished_member(1176, 'MERGEADO-FUERA')])
    ]
    red['errors'] = []

    green = copy.deepcopy(red)
    row = green['repos'][0]
    row['lote'].update(fase='aterrizando', fase_desde=NOW-40, ensayo=None)
    row['lote']['suite'][0].update(resultado='verde', fallas=0)
    row['espera'][0].update(etapa='admitido', motivo=None)
    row['recientes'].append(finished_batch(1, 7200))

    running = copy.deepcopy(red)
    row = running['repos'][0]
    row['lote'].update(fase='suite', fase_desde=NOW-420, ensayo=None)
    for run in row['lote']['suite']:
        run.update(estado='corriendo', inicio=NOW-420, fin=None, resultado=None, fallas=None)
    row['espera'] = row['recientes'] = []

    paused = copy.deepcopy(running)
    row = paused['repos'][0]
    row['pausa'] = dict(motivo='infraestructura', pr=None, desde=NOW-5700)
    row['lote'].update(fase='otra', fase_desde=NOW-5700)
    row['lote']['suite'][0].update(estado='pendiente-75', inicio=NOW-5700,
                                  lugar=dict(tipo='desconocido', vm=None, pueue_id=None, ref=None))
    row['lote']['suite'][1].update(estado='en-fila', inicio=None)

    empty = copy.deepcopy(red)
    for repo in empty['repos']:
        repo.update(lote=None, espera=[], recientes=[])

    errors = copy.deepcopy(red)
    errors['errors'] = [dict(stage='fases', message='Lectura fallida: OSError')]

    worst = copy.deepcopy(red)
    row = worst['repos'][0]
    row['lote']['miembros'].extend(dict(pr=p, head='1a2b3c4') for p in range(913, 917))
    row['lote']['ensayo'].update(modo='e2e', unidad=LONG_UNIT, inicio=NOW-6000,
                                prs=[912, 913], lugar=row['lote']['suite'][0]['lugar'])
    row['espera'] = [
        dict(pr=1000+i, head='123abcd', etapa=stage, desde=NOW-10*60,
             motivo={'retenido': 'ci-no-verde', 'caducado': 'head-cambiado'}.get(stage))
        for i, stage in enumerate(('entregado', 'admitido', 'retenido', 'caducado') + ('admitido',)*46)
    ]
    row['recientes'] = [finished_batch(i, i*3600) for i in (4, 2, 1, 3, 12)]
    row['recientes'][2]['miembros'].append(finished_member(906, 'CADUCADO'))

    hostile = copy.deepcopy(red)
    hostile['repos'][0]['lote']['ensayo']['unidad'] = HOSTILE

    return dict(rojo=red, verde=green, suite=running, pausa=paused, vacio=empty, etapas=errors,
                extremo=worst, texto=hostile)


def finished_member(pr, result):
    return dict(pr=pr, resultado=result, devolucion=None)


def finished_batch(i, ago):
    return dict(id=f'abc123dd{i:04x}', resultado='ATERRIZADO', cierre=NOW-ago-6000, fin=NOW-ago,
                miembros=[finished_member(1100+i, 'ATERRIZADO')])


def scenarios():
    """Todos los documentos del tren pasan por valid_tren y por la proyección real."""
    raw = documents()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        rack = contract.serve.Rack([], root, root/'no-linear', tren_repos=(REPO, SHADOW))
        rack.pulls = {
            REPO+'#912': dict(ticket='SEN-7301', title='Recibos de nómina: redondeo de ISR'),
            REPO+'#905': dict(ticket='SEN-7288', title='Rotar llaves API'),
            REPO+'#913': dict(ticket=None, title='Actualizar el escenario de pruebas de facturación'),
            SHADOW+'#1176': dict(ticket='SEN-7284', title='Recepciones parciales'),
        }
        snap = dict(contract.sample(), host='desk', taken=NOW,
                    cpu=dict(cores=8, load=[4, 3, 3]),
                    mem=dict(total=16*1024**3, available=9*1024**3, swap_total=0, swap_used=0),
                    tasks=[])
        running = contract.task(id=7812, start=NOW-420,
                                activity=dict(last=None, kind='chain', ref='job-abcDEF12'))
        estimate = dict(typical=780, low=660, high=900, n=5)
        hosts = [dict(id='desk', name='desk', fresh=True, age=0, estimates={'7812': estimate},
                      actions=dict(reason=None), snapshot=dict(snap, tasks=[running]))]

        projection = {}
        for name in ('rojo', 'verde', 'suite', 'pausa', 'vacio', 'etapas', 'extremo', 'texto',
                     'error', 'datos-antiguos', 'historial-antiguo', 'sin-repos'):
            source = 'extremo' if name == 'historial-antiguo' else name
            data = raw.get(source, raw['pausa'])
            validated = contract.tren.valid_tren(data, rack.tren_repos, NOW)
            rack.tren_good = None if name=='sin-repos' else (NOW, validated)
            rack.tren_error = 'No se pudo leer el tren' if name in ('error', 'sin-repos') else None
            now = NOW+19*60 if name in ('datos-antiguos', 'historial-antiguo') else NOW
            rack.pulls[REPO+'#912']['title'] = HOSTILE if name=='texto' else 'Recibos de nómina: redondeo de ISR'
            host_view = hosts if name in ('suite', 'extremo') else [dict(hosts[0], estimates={}, snapshot=snap)]
            projection[name] = dict(now=now, tickets={}, hosts=host_view, tren=rack.tren_view(host_view, now))

    projection['sin-tren'] = dict(now=NOW, tickets={}, hosts=projection['sin-repos']['hosts'])
    # El servidor rechaza esta URL. Se inyecta tras la proyección para probar también la guarda del DOM.
    projection['url-externa'] = copy.deepcopy(projection['rojo'])
    projection['url-externa']['tren']['repos'][0]['recientes'][0]['miembros'][0]['devolucion'][
        'comentario_url'] = 'https://example.org/comentario'
    return projection


def nodes(root, *, cls=None, tag=None):
    found = []
    for child in root['children']:
        if (cls is None or cls in child['cls'].split()) and (tag is None or child['tag'] == tag):
            found.append(child)
        found.extend(nodes(child, cls=cls, tag=tag))
    return found


class TrenUITests(unittest.TestCase):
    def view(self, name, expand=False):
        source = (BASE/'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        run = subprocess.run(['node', str(BASE/'tests/tren-ui-check.js')],
                             input=json.dumps(dict(js=source, fixture=scenarios()[name], expand=expand)),
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def section(self, name, expand=False):
        out = self.view(name, expand)
        self.assertEqual(out['writes'], [])
        self.assertEqual(out['order'][0], 'tren', 'Tren va antes de los hosts')
        self.assertEqual(len(out['sections']), 1)
        return out['sections'][0]

    def test_red_trial_and_return_with_comment_and_shadow(self):
        section = self.section('rojo')
        grid = nodes(section, cls='cols')[0]
        jale, hasito = grid['children']
        self.assertIn('Tren de Jale', jale['text'])
        self.assertIn('Buscando al culpable · 14 min', jale['text'])
        self.assertIn('Lote 7c1ea9d0e2b4 · cerrado hace 52 min', jale['text'])
        self.assertIn('SEN-7301Recibos de nómina: redondeo de ISR', jale['text'])
        self.assertEqual([n['text'] for n in nodes(jale, cls='stage-names')[0]['children']],
                         ['Lote', 'CI', 'Suite', 'Culpable', 'main'])
        self.assertEqual([n['text'] for n in nodes(jale, cls='now', tag='SPAN')], ['Culpable'])
        self.assertIn('Ensayo 3 · apps/api/prueba.contract.test.ts', jale['text'])
        self.assertIn('Repite con #912', jale['text'])
        self.assertNotIn('Ensayo 3 de', jale['text'])
        self.assertIn('desk · Pueue 7812', jale['text'])
        self.assertIn('Mini · Pueue 431', jale['text'])
        self.assertIn('Boat vm-prueba', jale['text'])
        self.assertIn('2 fallas · 12 min', jale['text'])
        self.assertEqual(nodes(jale, cls='bar'), [])
        self.assertIn('Retenido · ci-no-verde', jale['text'])
        self.assertIn('#905 SEN-7288 devuelto', jale['text'])
        self.assertEqual([(a['text'], a['href']) for a in nodes(jale, tag='A')],
                         [('contract · apps/api/prueba.contract.test.ts', COMMENT)])
        self.assertIn('Sombra · simula lotes, no corre suites', hasito['text'])
        self.assertIn('Lote simulado · 1 PR', hasito['text'])
        self.assertIn('mergeado fuera del tren', hasito['text'])

    def test_green_landing_and_landed(self):
        section = self.section('verde')
        self.assertIn('Aterrizando en main · menos de 1 min', section['text'])
        self.assertIn('Verde · 12 min', section['text'])
        self.assertIn('En espera · 1 PR', section['text'])
        self.assertIn('Admitido · entra en el próximo lote', section['text'])
        self.assertIn('Aterrizó · 1 PR', section['text'])

    def test_running_progress_uses_joined_estimate_only(self):
        section = self.section('suite')
        desk, mini = nodes(section, cls='run')
        self.assertIn('7 min de ~13 min', desk['text'])
        self.assertEqual(len(nodes(desk, cls='bar')), 1)
        self.assertIn('Mini · Pueue 431', mini['text'])
        self.assertIn('7 min', mini['text'])
        self.assertEqual(nodes(mini, cls='bar'), [])

    def test_pause_stale_server_error_and_partial_read(self):
        paused = self.section('pausa')['text']
        self.assertIn('En pausa · infraestructura', paused)
        self.assertIn('Fase otra', paused)
        self.assertIn('Sin lugar confirmado', paused)
        self.assertIn('Sin terminar · salida 75 · 1.6 h', paused)
        self.assertIn('En fila', paused)
        self.assertIn('Sin datos del tren desde hace 19 min', self.section('datos-antiguos')['text'])
        self.assertIn('No se pudo leer el tren', self.section('error')['text'])
        errors = self.section('etapas')['text']
        self.assertIn('No se pudo leer: fases', errors)
        self.assertNotIn('OSError', errors)

    def test_empty_repos_and_absent_section(self):
        empty = self.section('vacio')['text']
        self.assertIn('Sin PRs entregados.', empty)
        self.assertIn('Sin PRs en custodia.', empty)
        absent, no_repos = self.view('sin-tren'), self.view('sin-repos')
        self.assertEqual(absent['sections'], [])
        self.assertEqual(absent, no_repos)
        self.assertEqual(absent['order'], ['stack'])

    def test_data_enters_only_as_text(self):
        section = self.section('texto')
        self.assertTrue(any(n['text']==HOSTILE for n in nodes(section, cls='t')))
        self.assertTrue(any(n['text']==HOSTILE for n in nodes(section, cls='unit')))
        self.assertEqual(nodes(section, tag='IMG'), [])

    def test_comment_only_links_to_github(self):
        section = self.section('url-externa')
        self.assertEqual(nodes(section, tag='A'), [])
        self.assertIn('contract · apps/api/prueba.contract.test.ts', section['text'])

    def test_worst_case_and_expansion_limits(self):
        jale = nodes(self.section('extremo'), cls='host')[0]
        members, waiting, finished = nodes(jale, tag='OL')
        self.assertEqual([len(row['children']) for row in (members, waiting, finished)], [5, 8, 3])
        self.assertIn('Sin ticketActualizar el escenario de pruebas de facturación', jale['text'])
        self.assertIn('Ensayo 3 · '+LONG_UNIT, jale['text'])
        self.assertIn('1.7 h · suele tardar', jale['text'])
        self.assertEqual(len(nodes(nodes(jale, cls='run')[-1], cls='bar')), 1)
        self.assertIn('En espera · 50 PRs', jale['text'])
        for label in ('Entregado', 'Admitido', 'Retenido · ci-no-verde', 'Caducado · head-cambiado',
                      'Aterrizó · 2 PRs', 'Aterrizó · 1 PR', '#906 caducado'):
            self.assertIn(label, jale['text'])
        self.assertEqual([b['text'] for b in nodes(jale, tag='BUTTON')], ['Ver 42 más', 'Ver 2 más'])
        self.assertEqual([row['children'][1]['text'] for row in finished['children']],
                         ['hace 60 min · 1.7 h', 'hace 2.0 h · 1.7 h', 'hace 3.0 h · 1.7 h'])
        expanded = nodes(self.section('extremo', expand=True), cls='host')[0]
        self.assertEqual([len(row['children']) for row in nodes(expanded, tag='OL')], [5, 50, 5])
        self.assertEqual(nodes(expanded, tag='BUTTON'), [])
        self.assertIn('hace 12.0 h', expanded['text'])

    def test_old_documents_hide_finished_batches_outside_twelve_hours(self):
        jale = nodes(self.section('historial-antiguo'), cls='host')[0]
        self.assertEqual([b['text'] for b in nodes(jale, tag='BUTTON')], ['Ver 42 más', 'Ver 1 más'])
