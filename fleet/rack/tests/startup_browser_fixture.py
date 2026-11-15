"""Offline HTML from synthetic collect -> validate -> publish. No real host."""
import json
from pathlib import Path
import sys
from test_startup import StartupPipelineTests, MiniStartupPipelineTests
from test_slots import BASE


def main():
    interrupted=StartupPipelineTests().fixture()
    normal=StartupPipelineTests().fixture(normal=True)
    mini=MiniStartupPipelineTests().fixture()
    scenarios=dict(interrumpida=interrupted,normal=normal,mini=mini)
    script='''
window.useStartupScenario=function(name){
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();
  render(structuredClone(STARTUP_SCENARIOS[name]));
  return {scenario:name,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    hold:[...document.querySelectorAll('.slot.held')].map(e=>e.textContent).find(t=>t.startsWith('Retenido al reiniciar')),
    waits:[...document.querySelectorAll('.slot.held')].filter(e=>e.textContent.startsWith('Esperando cuotas y Docker')).length,
    sensorWaits:[...document.querySelectorAll('.slot.held')].filter(e=>e.textContent.startsWith('Esperando cuotas y sensores')).length,
    groups:[...document.querySelectorAll('.lane > .g')].map(e=>e.firstChild.textContent.trim()),
    interrupted:document.querySelector('.interrupted')?.textContent};
};
window.fetch=async()=>{throw Error('Synthetic fixture: network forbidden')};
useStartupScenario('interrumpida');
'''
    source=(BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const STARTUP_SCENARIOS='+json.dumps(scenarios,ensure_ascii=False).replace('</','<\\/')+';\n'+script)
    output=Path(sys.argv[1]);output.write_text(source)
    print(output)


if __name__=='__main__':main()
