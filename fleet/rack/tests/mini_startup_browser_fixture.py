"""Offline Mini synthetic page. No server, shared host, installed config or network."""
import json
import copy
from pathlib import Path
import sys
from test_mini_startup import MiniStartupTests
from test_slots import BASE


def main():
    held=MiniStartupTests().fixture()
    interrupted=copy.deepcopy(held)
    snap=interrupted['hosts'][0]['snapshot']
    snap['control'].update(startup_holds={},startup_waiting={},pause_reasons={g['name']:[] for g in snap['groups']})
    for group in snap['groups']:group['status']='Running'
    fixtures=dict(retenida=held,interrumpida=interrupted,normal=MiniStartupTests().fixture(normal=True))
    script='''
window.useMiniScenario=function(name){
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();render(structuredClone(MINI_SCENARIOS[name]));
  return {scenario:name,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    hold:[...document.querySelectorAll('.slot.held')].map(e=>e.textContent).find(t=>t.startsWith('Retenido al reiniciar')),
    interrupted:[...document.querySelectorAll('.interrupted-summary')].map(e=>({text:e.textContent,expanded:e.getAttribute('aria-expanded')})),
    sensors:document.getElementById('app').textContent.includes('Esperando cuotas y sensores'),docker:document.getElementById('app').textContent.includes('Esperando cuotas y Docker')};
};
window.fetch=async()=>{throw Error('Synthetic: network forbidden')};
useMiniScenario('interrumpida');
'''
    source=(BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const MINI_SCENARIOS='+json.dumps(fixtures,ensure_ascii=False).replace('</','<\\/')+';\n'+script)
    Path(sys.argv[1]).write_text(source)
    print(sys.argv[1])


if __name__=='__main__':main()
