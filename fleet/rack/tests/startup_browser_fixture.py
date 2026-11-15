"""Offline HTML from synthetic collect -> validate -> publish. No real host."""
import json
import copy
from pathlib import Path
import sys
from test_startup import StartupPipelineTests
from test_slots import BASE


def folded_fixture(interrupted):
    """Ten synthetic attributed cuts, using the production pipeline's payload."""
    data=copy.deepcopy(interrupted)
    host=data['hosts'][0];snap=host['snapshot'];control=snap['control']
    task=snap['tasks'][0]
    snap['tasks']=[dict(task,id=100+i,group='pesadas',name='e2e' if i%2 else 'contract',
        ticket='SEN-'+str(7100+i) if i%3 else None,
        subject='Comprobar recuperación de la cola '+str(i+1) if i%3 else None,
        branch='feat/recuperacion-'+str(i+1),end=data['now']-(7200 if i<5 else 3300)) for i in range(10)]
    control['startup_holds']={'otros':'pausa manual','jale-verify':'tarea en fila con log previo; lanzamiento ambiguo'}
    control['startup_waiting']={g:v for g,v in control['startup_waiting'].items() if g not in ('otros','jale-verify','pesadas')}
    control['pause_reasons'].update(otros=['manual'],**{'jale-verify':['startup','ambiguous'],'pesadas':[]})
    for group in snap['groups']:
        if group['name']=='pesadas':group['status']='Running'
    return data


def main():
    interrupted=StartupPipelineTests().fixture()
    normal=StartupPipelineTests().fixture(normal=True)
    scenarios=dict(interrumpida=interrupted,normal=normal,plegada=folded_fixture(interrupted))
    script='''
window.useStartupScenario=function(name){
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();
  render(structuredClone(STARTUP_SCENARIOS[name]));
  return {scenario:name,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    holds:[...document.querySelectorAll('.slot.held')].map(e=>e.textContent),
    waits:[...document.querySelectorAll('.slot.held')].filter(e=>e.textContent.startsWith('Esperando cuotas y Docker')).length,
    interrupted:[...document.querySelectorAll('.interrupted-summary')].map(e=>({text:e.textContent,expanded:e.getAttribute('aria-expanded')}))};
};
window.fetch=async()=>{throw Error('Synthetic fixture: network forbidden')};
useStartupScenario('interrumpida');
'''
    source=(BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const STARTUP_SCENARIOS='+json.dumps(scenarios,ensure_ascii=False).replace('</','<\\/')+';\n'+script)
    output=Path(sys.argv[1]);output.write_text(source)
    print(output)


if __name__=='__main__':main()
