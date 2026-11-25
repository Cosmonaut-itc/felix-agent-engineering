"""Franja de procesos sin tarea: JS de producción con DOM sintético; ningún host ni red."""
import safety
import copy
import json
import subprocess
import time
import unittest
from test_rack import serve

BASE = serve.BASE
NOW = 1794777600  # 2026-11-15 20:00 UTC
GiB = 1024**3
HOSTILE = '<img src=x onerror=alert(1)>'


def row(pid, comm, task, group, state, rss, cpu, ago):
    started = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW - ago))
    return dict(pid=pid, comm=comm, origin_task=task, group=group, origin_state=state,
                rss_bytes=int(rss * GiB), cpu_percent=cpu, started_at=started)


def scenarios():
    """Snapshots que el validador del servidor acepta, por nombre de escenario."""
    restos = [row(51877, 'node', '1642', 'pesadas', 'Killed', 2.3, 48, 38*60),
              row(48211, 'node', '1619', 'jale', 'Done', 1.4, 35, 2*3600),
              row(51903, 'chrome', '1642', 'pesadas', 'Killed', 0.9, 12, 37*60),
              row(20455, 'python3', None, None, 'desconocido', 0.3, 3, 5*3600)]
    muchos = [row(60000 + i, 'worker', str(1700 + i), 'otros', state, 1.0 - i/10, None, 600*(i+1))
              for i, state in enumerate(('Done', 'Killed', 'Running', 'desconocido', 'Done'))]
    cortados = [row(70000 + i, 'node', str(1800 + i % 2), 'jale', 'Done', 1 - i/100, 1, 900) for i in range(64)]
    result = {}
    for name, rows, total in (('con-restos', restos, 4), ('sin-restos', [], 0), ('muchos-origenes', muchos, 5),
                              ('cortados', cortados, 70), ('sin-campos', None, None)):
        snap = dict(schema='rack-host@1', host='desk', taken=NOW, tasks=[],
                    groups=[dict(name='jale', slots=2, status='Running')],
                    cpu=dict(cores=16, load=[17.6, 17, 16]),
                    mem=dict(total=32*GiB, available=7.8*GiB, swap_total=8*GiB, swap_used=0.4*GiB))
        if rows is not None:
            snap.update(orphans=rows, orphans_total=total)
        result[name] = serve.validate_snapshot(snap, 'desk')
    return result


def payload(snap):
    return dict(now=NOW, tickets={}, hosts=[dict(id='desk', name='Desk · WSL', fresh=True, age=0,
                estimates={}, actions=dict(reason=None), snapshot=snap)])


class OrphanUITests(unittest.TestCase):
    def view(self, name, open=False, mutate=None):
        snap = copy.deepcopy(scenarios()[name])
        if mutate:
            mutate(snap)
        source = (BASE/'index.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        run = subprocess.run(['node', str(BASE/'tests/orphans-ui-check.js')],
                             input=json.dumps(dict(js=source, fixture=payload(snap), open=open)),
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def strip(self, out):
        self.assertEqual(len(out['boxes']), 1, 'la franja debe aparecer una vez')
        self.assertEqual(out['writes'], [], 'la franja se construye sólo con textContent')
        box = out['boxes'][0]
        summary, rows = box['children'][0], box['children'][1]
        return dict(count=summary['children'][0]['children'][0]['text'], sub=summary['children'][0]['children'][1]['text'],
                    rows=[dict(head=li['children'][0]['text'], n=li['children'][1]['text'], l2=li['children'][2]['text'],
                               title=li['children'][2]['children'][-1]['title']) for li in rows['children']],
                    more=box['children'][2:])

    def test_with_orphans_between_capacity_and_lanes_grouped_by_origin(self):
        out = self.view('con-restos')
        html = out['html']
        self.assertIn('data-orphans="desk"', html)
        self.assertLess(html.index('class="cap"'), html.index('data-orphans="desk"'))
        self.assertLess(html.index('data-orphans="desk"'), html.index('class="lanes"'))
        s = self.strip(out)
        self.assertEqual(s['count'], '4 procesos sin tarea')
        self.assertEqual(s['sub'], 'Siguen consumiendo 4,9 GB y 1 núcleo · Pueue ya liberó su lugar')
        self.assertEqual([r['head'] for r in s['rows']],
                         ['1642 · pesadas · Matada', '1619 · jale · Terminó', 'Sin atribuir · origen desconocido'])
        self.assertEqual([r['n'] for r in s['rows']], ['3,2 GB · 60 % CPU', '1,4 GB · 35 % CPU', '0,3 GB · 3 % CPU'])
        self.assertEqual([r['l2'] for r in s['rows']],
                         ['node 51877 · chrome 51903 · hace 38 min', 'node 48211 · hace 2.0 h', 'python3 20455 · hace 5.0 h'])
        self.assertTrue(all(r['title'].startswith('Desde ') for r in s['rows']))
        self.assertEqual(s['more'], [])
        self.assertNotIn('OOM', json.dumps(out))

    def test_data_enters_only_as_text(self):
        def hostile(snap):
            snap['orphans'][0]['comm'] = HOSTILE
            snap['orphans'][1]['group'] = HOSTILE
        out = self.view('con-restos', mutate=hostile)
        s = self.strip(out)
        self.assertIn(HOSTILE + ' 51877', s['rows'][0]['l2'])
        self.assertIn(HOSTILE, s['rows'][1]['head'])
        self.assertNotIn(HOSTILE, out['html'])

    def test_nothing_without_orphans_or_without_fields(self):
        for name in ('sin-restos', 'sin-campos'):
            with self.subTest(name):
                out = self.view(name)
                self.assertNotIn('data-orphans', out['html'])
                self.assertNotIn('sin tarea', out['html'])
                self.assertEqual(out['boxes'], [])

    def test_more_than_three_origins_fold_like_the_queue(self):
        s = self.strip(self.view('muchos-origenes'))
        self.assertEqual(s['count'], '5 procesos sin tarea')
        self.assertEqual(s['sub'], 'Siguen consumiendo 4,0 GB y — núcleos · Pueue ya liberó su lugar')
        self.assertEqual([r['head'] for r in s['rows']],
                         ['1700 · otros · Terminó', '1701 · otros · Matada', '1702 · otros · Corriendo'])
        self.assertEqual(s['rows'][0]['n'], '1,0 GB · — % CPU')
        self.assertEqual([(b['tag'], b['type'], b['text'], b['open']) for b in s['more']],
                         [('BUTTON', 'button', 'Ver 2 más', '["orphans","desk"]')])
        opened = self.strip(self.view('muchos-origenes', open=True))
        self.assertEqual([r['head'] for r in opened['rows']][3:], ['1703 · otros · desconocido', '1704 · otros · Terminó'])
        self.assertEqual(opened['more'], [])

    def test_total_beyond_the_list_is_said(self):
        s = self.strip(self.view('cortados'))
        self.assertEqual(s['count'], '64 procesos sin tarea y 6 más que no caben')
        self.assertEqual([r['head'] for r in s['rows']], ['1800 · jale · Terminó', '1801 · jale · Terminó'])


if __name__ == '__main__':
    unittest.main()
