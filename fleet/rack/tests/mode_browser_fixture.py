"""Emit self-contained synthetic UI for screenshots, never starts a server.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/mode_browser_fixture.py OUTPUT.html
The browser has no collector, controller or network access from this fixture.
"""
import copy
import json
from pathlib import Path
import sys
from test_mode import ModeChannelTests
from test_slots import control, FakePueue, BASE


def scenarios():
    result={}
    def make(name,when,mode=None,pending=None,message=None):
        test=ModeChannelTests();test.setUp()
        try:
            test.now=when;test.engine.reconcile()
            if mode:
                answer=test.engine.handle(dict(schema=control.HOST_SCHEMA,op='mode',action_id='fixture-mode-001',dry_run=False,
                    expected={k:test.engine.view()[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(mode=mode)))
                assert answer['status']=='applied',answer
            fixture=test.fixture()
            result[name]=dict(fixture=fixture,pending=pending,message=message)
        finally:test.doCleanups()
    night=1794664799.0
    day=1794693600.0  # 2026-11-14 22:00 UTC
    make('automatico-dia',day)
    make('automatico-noche',night)
    make('adelantado-noche',day,'noche')
    make('adelantado-dia',night,'dia')
    for pending in ('applying','partial','unknown'):make(pending,day,pending=pending)
    make('conflicto',day,message='El horario o los lugares cambiaron; no se puede deshacer este ajuste')
    make('vencido',1794718800.,message='El ajuste venció en el cambio de horario.')
    make('trabajo-ajeno',day)
    result['trabajo-ajeno']['fixture']['hosts'][0]['snapshot']['control']['foreign_work']=1
    make('host-antiguo',day)
    old=result['host-antiguo']['fixture']['hosts'][0]['snapshot']['control'];old['capabilities']['mode']=False
    for k in ('mode_note','mode_detail','mode_pending','mode_partial','mode_unknown'):old.pop(k,None)
    mini=copy.deepcopy(result['automatico-dia'])
    host=mini['fixture']['hosts'][0];host.update(id='mini',name='Mac Mini')
    host['snapshot']['control'].update(mode='fijo',cap=7,slots=control.MINI_PROFILE,desired=control.MINI_PROFILE)
    host['snapshot']['control']['capabilities']['mode']=False
    host['snapshot']['groups']=[dict(name=g,slots=n,status='Running') for g,n in control.MINI_PROFILE.items()]
    result['mini']=mini
    return result


def main():
    output=Path(sys.argv[1]).resolve()
    data=scenarios()
    fixture_js='''
window.useModeScenario = function(name){
  const scenario=MODE_SCENARIOS[name], fixture=structuredClone(scenario.fixture);
  PENDING.clear();SLOT_MESSAGES.clear();HOST_CONFIRMED.clear();
  if(scenario.pending){
    const c=fixture.hosts[0].snapshot.control;
    PENDING.set('@host/desk',{mode:true,status:scenario.pending,
      request:{op:'mode',host:'desk',action_id:'browser-mode-001',change:{mode:'noche'}},
      result:{observed:c.slots,desired:{hasito:2,jale:7,'jale-verify':1,otros:2,pesadas:3,rapidas:3}}});
  }
  if(scenario.message) SLOT_MESSAGES.set(fixture.hosts[0].id,scenario.message);
  render(fixture);document.getElementById('toast').textContent='';
  return {scenario:name,viewport:innerWidth,scrollWidth:document.documentElement.scrollWidth,
    buttons:[...document.querySelectorAll('[data-mode]')].map(b=>({mode:b.dataset.mode,selected:b.getAttribute('aria-pressed'),disabled:b.disabled})),
    state:document.querySelector('.action-state')?.textContent || '',note:document.querySelector('.mode-note')?.textContent || ''};
};
window.fetch=async()=>{throw Error('Synthetic fixture: network forbidden')};
useModeScenario('automatico-dia');
'''
    source=(BASE/'index.html').read_text().replace('refresh(); setInterval(refresh,15000);',
        'const MODE_SCENARIOS='+json.dumps(data,ensure_ascii=False)+';\n'+fixture_js)
    output.write_text(source)
    output.with_suffix('.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(str(output))


if __name__=='__main__':main()
