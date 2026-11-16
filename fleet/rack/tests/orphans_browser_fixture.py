"""Página sin servidor con la franja de procesos sin tarea, para capturas.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/orphans_browser_fixture.py OUTPUT.html
En el navegador: useOrphansScenario('con-restos'|'sin-restos'|'sin-campos'|'muchos-origenes'|'cortados', 'light'|'dark').
"""
import json
from pathlib import Path
import sys
from test_mode import ModeChannelTests
from test_orphans_ui import NOW, scenarios, BASE


def desk(snap):
    test = ModeChannelTests(); test.setUp()
    try:
        test.now = NOW; test.engine.reconcile(); data = test.fixture()
    finally:
        test.doCleanups()
    data['now'] = NOW
    host = data['hosts'][0]['snapshot']
    task = lambda i, group, ticket, subject, ago, state='Running': dict(id=i, group=group, state=state, name='e2e',
        ticket=ticket, subject=subject, branch=None, start=NOW-ago if state == 'Running' else None, enqueued=NOW-ago-60, priority=0)
    host['tasks'] = [task(1, 'hasito', 'SEN-6490', 'Corte de caja con descuentos', 720),
                     task(2, 'jale', 'SEN-6471', 'Turnos rotativos en el checador', 1860),
                     task(3, 'pesadas', 'SEN-6475', 'Reporte de inventario por sucursal', 1320),
                     task(4, 'pesadas', 'SEN-6488', 'Gates de facturación CFDI', 840, 'Queued')]
    host['cpu'], host['mem'] = snap['cpu'], snap['mem']
    for key in ('orphans', 'orphans_total'):
        if key in snap:
            host[key] = snap[key]
    return data


def main():
    output = Path(sys.argv[1]).resolve()
    data = {name: desk(snap) for name, snap in scenarios().items()}
    script = '''
window.useOrphansScenario=function(name,theme){
  document.documentElement.dataset.theme=theme || 'light';
  for(const k of Object.keys(OPEN)) delete OPEN[k];
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();
  render(structuredClone(ORPHAN_SCENARIOS[name]));
  const strip=document.querySelector('[data-orphans]');
  return {scenario:name,theme,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    strip:strip ? strip.innerText : null};
};
window.fetch=async()=>{throw Error('Synthetic fixture: network forbidden')};
useOrphansScenario('con-restos');
'''
    source = (BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const ORPHAN_SCENARIOS='+json.dumps(data, ensure_ascii=False).replace('</', '<\\/')+';\n'+script)
    output.write_text(source)
    print(output)


if __name__ == '__main__':
    main()
