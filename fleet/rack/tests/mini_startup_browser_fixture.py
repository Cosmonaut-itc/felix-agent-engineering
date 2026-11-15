"""Offline Mini synthetic page. No server, shared host, installed config or network."""
import json
from pathlib import Path
import sys
from test_mini_startup import MiniStartupTests
from test_slots import BASE


def main():
    fixtures={name:MiniStartupTests().fixture(normal=normal) for name,normal in (('interrumpida',False),('normal',True))}
    script='''
window.useMiniScenario=function(name){
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();render(structuredClone(MINI_SCENARIOS[name]));
  return {scenario:name,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    hold:[...document.querySelectorAll('.slot.held')].map(e=>e.textContent).find(t=>t.startsWith('Retenido al reiniciar')),
    interrupted:document.querySelector('.interrupted')?.textContent};
};
window.fetch=async()=>{throw Error('Synthetic: network forbidden')};
useMiniScenario('interrumpida');
'''
    source=(BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const MINI_SCENARIOS='+json.dumps(fixtures,ensure_ascii=False).replace('</','<\\/')+';\n'+script)
    Path(sys.argv[1]).write_text(source)
    print(sys.argv[1])


if __name__=='__main__':main()
