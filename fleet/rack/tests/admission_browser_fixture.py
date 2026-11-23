"""Página sintética sin servidor para revisar admisión con el HTML de producción."""

import json
from pathlib import Path
import sys

from test_admission import AdmissionTests, BASE, RESOURCE


def scenarios():
    test = AdmissionTests()
    try:
        test.setUp()
        native = test.payload(test.collect())
        test.write_projection()
        admitted = test.payload(test.collect(), reorder=True)
        resources = ['postgres:' + 'x' * 55, 'postgres:' + 'y' * 55, RESOURCE]
        test.waiting.update(resources=resources, positions=dict.fromkeys(resources, 2))
        test.waiting['blockers'] = [dict(test.waiting['blockers'][0], resource=n) for n in resources]
        name = 'dirigida-' + 'x' * 119
        test.waiting['ref'] = name
        test.managed['label'] = 'run=' + name
        test.legacy['status'] = {'Paused': test.legacy['status']['Running']}
        test.pueue['groups']['suites']['status'] = 'Paused'
        test.write_projection()
        worst = test.payload(test.collect())
        return {'con-admision': admitted, 'sin-admision': native, 'texto-largo': worst}
    finally:
        test.doCleanups()


def main():
    output = Path(sys.argv[1]).resolve()
    source = (BASE / 'index.html').read_text(encoding='utf-8')
    data = json.dumps(scenarios(), ensure_ascii=False).replace('</', '<\\/')
    script = '''
window.useAdmissionScenario = function(name, theme){
  document.documentElement.dataset.theme = theme || 'light';
  for(const key of Object.keys(OPEN)) delete OPEN[key];
  PENDING.clear(); SLOT_MESSAGES.clear(); HOST_CONFIRMED.clear();
  render(structuredClone(ADMISSION_SCENARIOS[name]));
};
window.fetch = async () => { throw Error('Synthetic fixture: network forbidden'); };
const query = new URLSearchParams(location.search);
useAdmissionScenario(query.get('scenario') || 'con-admision', query.get('theme'));
'''
    output.write_text(source.replace('refresh(); setInterval(refresh,15000);',
                                    'const ADMISSION_SCENARIOS = ' + data + ';\n' + script),
                      encoding='utf-8')
    print(output.name)


if __name__ == '__main__':
    main()
