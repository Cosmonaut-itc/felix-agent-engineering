"""Offline HTML from synthetic collect -> validate -> publish. No real host."""
import json
from pathlib import Path
import sys
from test_startup import StartupPipelineTests
from test_slots import BASE


def main():
    interrupted=StartupPipelineTests().fixture()
    normal=StartupPipelineTests().fixture(normal=True)
    scenarios=dict(interrumpida=interrupted,normal=normal)
    script='''
window.useStartupScenario=function(name){
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();
  render(structuredClone(STARTUP_SCENARIOS[name]));
  return {scenario:name,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    hold:document.querySelector('.startup-hold')?.textContent,
    waits:document.querySelectorAll('.startup-wait').length,
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
