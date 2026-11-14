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
    def make(name,when,mode=None,pending=None,message=None,slots=False,undo_target=None):
        test=ModeChannelTests();test.setUp()
        try:
            test.now=when;test.engine.reconcile()
            if mode:
                answer=test.engine.handle(dict(schema=control.HOST_SCHEMA,op='mode',action_id='fixture-mode-001',dry_run=False,
                    expected={k:test.engine.view()[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(mode=mode)))
                assert answer['status']=='applied',answer
            if slots:
                view=test.engine.view()
                answer=test.engine.handle(dict(schema=control.HOST_SCHEMA,op='slots',action_id='fixture-slots01',dry_run=False,
                    expected={k:view[k] for k in ('revision','policy_hash','generation','mode','slots')},change=dict(group='jale',delta=-1)))
                assert answer['status']=='applied',answer
            fixture=test.fixture()
            result[name]=dict(fixture=fixture,pending=pending,message=message,undo_target=undo_target)
        finally:test.doCleanups()
    night=1794664799.0
    day=1794693600.0  # 2026-11-14 22:00 UTC
    make('automatico-dia',day)
    make('automatico-noche',night)
    make('adelantado-noche',day,'noche')
    make('adelantado-dia',night,'dia')
    for pending in ('applying','partial','unknown'):make(pending,day,pending=pending)
    make('conflicto',day,message='El horario o los lugares cambiaron; no se puede deshacer este ajuste')
    make('conflicto-con-lugares',day,'noche',slots=True,message='El horario o los lugares cambiaron; no se puede deshacer este ajuste')
    make('undo-a-dia',day,'noche',pending='applying',undo_target='dia')
    make('undo-a-noche',night,'dia',pending='applying',undo_target='noche')
    make('unknown-recargado',day,'noche',pending='unknown',undo_target='dia')
    make('parcial-con-ajustes',day,'noche',slots=True,pending='partial',undo_target='dia')
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
    adjusted=copy.deepcopy(mini)
    c=adjusted['fixture']['hosts'][0]['snapshot']['control']
    c['slots_note']='Lugares ajustados · hasta otro cambio manual'
    c['slots']=dict(control.MINI_PROFILE,suites=2);c['desired']=dict(c['slots'])
    adjusted['fixture']['hosts'][0]['snapshot']['groups']=[dict(name=g,slots=n,status='Running') for g,n in c['slots'].items()]
    adjusted['message']='El horario o los lugares cambiaron; no se puede deshacer este ajuste'
    result['mini-conflicto-con-lugares']=adjusted
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
    const target=scenario.undo_target || 'noche';
    const request={op:scenario.undo_target?'undo':'mode',host:'desk',action_id:'browser-mode-001'};
    if(scenario.undo_target) request.undo_of='browser-before1';else request.change={mode:target};
    const action={mode:true,targetMode:target,from:c.mode,status:scenario.pending,request,
      result:{observed:c.slots,desired:c.tables[target]}};
    // Restore through the same persisted JSON shape as a reloaded tab.
    sessionStorage.setItem('rack-actions',JSON.stringify([['@host/desk',action]]));
    PENDING.set('@host/desk',JSON.parse(sessionStorage.getItem('rack-actions'))[0][1]);
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
