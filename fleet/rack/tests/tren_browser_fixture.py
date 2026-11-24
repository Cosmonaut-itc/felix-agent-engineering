"""Genera la página sintética con el render de producción para capturas del Tren."""
import json
from pathlib import Path
import sys

from test_tren_ui import BASE, scenarios

CONTROLS = '''<label>Escenario <select id="fixture-scenario"></select></label>
<label>Tema <select id="fixture-theme"><option value="light">Claro</option>
<option value="dark">Oscuro</option></select></label>'''

SCRIPT = '''
window.fetch=async()=>{throw Error('Fixture sintético sin red')};
const scenarioControl=document.getElementById('fixture-scenario');
const themeControl=document.getElementById('fixture-theme');
for(const name of Object.keys(TREN_SCENARIOS)){
  scenarioControl.add(new Option(name,name));
}
window.useTrenScenario=function(name='rojo',theme='light'){
  scenarioControl.value=name;
  themeControl.value=theme;
  document.documentElement.dataset.theme=theme;
  for(const key of Object.keys(OPEN)) delete OPEN[key];
  render(structuredClone(TREN_SCENARIOS[name]));
  return {scenario:name,theme,width:innerWidth,scrollWidth:document.documentElement.scrollWidth};
};
const repaintFixture=()=>useTrenScenario(scenarioControl.value,themeControl.value);
scenarioControl.addEventListener('change',repaintFixture);
themeControl.addEventListener('change',repaintFixture);
const params=new URLSearchParams(location.search);
useTrenScenario(params.get('e') || 'rojo',params.get('tema') || 'light');
'''


def main():
    output = Path(sys.argv[1]).resolve()
    data = json.dumps(scenarios(), ensure_ascii=False).replace('<', r'\u003c')
    source = (BASE/'index.html').read_text()
    source = source.replace('</header>', CONTROLS+'</header>', 1)
    source = source.replace('refresh(); setInterval(refresh,15000);',
                            'const TREN_SCENARIOS='+data+';\n'+SCRIPT, 1)
    output.write_text(source)
    print(output)


if __name__ == '__main__':
    main()
