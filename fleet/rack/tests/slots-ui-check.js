// Production JS; synthetic DOM/network/storage. No browser or server required.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},storage={},timers=[],requests=[],listeners=[];
const el=()=>({innerHTML:'',textContent:'',addEventListener(){},focus(){},querySelector(){return {onclick:null}}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(type,fn){listeners.push([type,fn])},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},
 fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
const run=s=>vm.runInContext(s,context);
run('render(fixture);globalThis.h=S.hosts[0]');
(async()=>{
 if(input.scenario==='desk'){
   for(const [mode,cap,slots] of [['dia',14,{hasito:1,jale:5,'jale-verify':1,otros:2,pesadas:2,rapidas:3}],
                                ['noche',18,{hasito:2,jale:7,'jale-verify':1,otros:2,pesadas:3,rapidas:3}]]){
     context.c={...run('h.control'),mode,cap,slots,desired:slots};
     run('h.control=c;for(const [g,n] of Object.entries(c.slots)) h.groups[g]={slots:n}');
     const full=run("slotControls(h,'jale',h.groups.jale,0)");
     assert.match(full,new RegExp(`Tope de ${cap} lugares. Quita un lugar a otro grupo`));
     assert.match(full,/data-step="1"[^>]*disabled/);
     assert(!/data-step="-1"[^>]*disabled/.test(full));
     assert.match(run("slotControls(h,'jale-verify',h.groups['jale-verify'],0)"),/Mínimo de 1 lugar/);
     assert.match(run("slotControls({...h,control:{...c,slots:{...c.slots,jale:8}}},'jale',{slots:8},0)"),/Máximo de 8 lugares/);
     assert.match(run("slotControls({...h,control:null},'jale',h.groups.jale,0)"),/Falta actualizar/);
     assert.match(run("slotControls(h,'default',{slots:1},0)"),/disabled/);
     assert.match(run("slotControls(h,'default',{slots:1},0)"),/Reservado · fuera del reparto/);
     assert.doesNotMatch(run("slotControls(h,'default',{slots:1},0)"),/Falta actualizar/);
     run("h.control.slots.jale--;h.groups.jale.slots--");
     assert(!/data-step="1"[^>]*disabled/.test(run("slotControls(h,'jale',h.groups.jale,0)")));
     const foreign=run("slotControls({...h,control:{...h.control,foreign_work:1}},'jale',h.groups.jale,0)");
     assert.match(foreign,/trabajo fuera del reparto/);
     assert.match(foreign,/data-step="1"[^>]*disabled/);
     assert(!/data-step="-1"[^>]*disabled/.test(foreign));
     for(const pending of [{drift:true},{recovery_pending:true},{drift:true,recovery_pending:true}]){
       context.foreignHost={...run('h'),control:{...run('h.control'),foreign_work:1,...pending}};
       const buttons=run("slotControls(foreignHost,'jale',foreignHost.groups.jale,0)");
       assert(!/data-step="-1"[^>]*disabled/.test(buttons),'foreign work must allow reduction during reconciliation');
       assert.match(buttons,/data-step="1"[^>]*disabled/);
       assert.match(run('hostControlState(foreignHost)'),/Hay trabajo fuera del reparto/);
     }
   }
   run('render(fixture);globalThis.h=S.hosts[0]');
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('lost')};
   run("globalThis.sent=null;const oldSend=sendSlots;sendSlots=(...a)=>(globalThis.sent=oldSend(...a));startSlots(h,{group:'jale',delta:-1})");
   assert.match(elements.app.innerHTML,/Aplicando/);
   await run('sent');assert.match(elements.app.innerHTML,/Resultado desconocido/);
   run('globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)');
   const ident=run('a.request.action_id');
   context.fetch=async()=>({ok:false,status:409,json:async()=>({schema:'rack-action@1',action_id:ident,status:'conflict',error:{message:'Los lugares cambiaron'}})});
   await run('pollAction(key,a)');assert.equal(run('PENDING.size'),0);assert.match(elements.app.innerHTML,/Los lugares cambiaron/);
   run('SLOT_MESSAGES.clear();render(fixture)');
   context.answer={schema:'rack-action@1',action_id:'confirm-000001',status:'applied',revision:2,
     result:{observed:{...run('h.control.slots'),jale:6},desired:{...run('h.control.slots'),jale:6},expires_at:1794664800,slots_note:'Lugares ajustados · hasta las 08:00'}};
   run("acceptSlots(slotKey(h),{request:{op:'slots',host:'desk',action_id:'confirm-000001'},group:'jale'},answer)");
   assert.match(elements.toast.innerHTML,/hasta las 08:00/);assert.match(elements.toast.innerHTML,/Deshacer/);
   assert.doesNotMatch(elements.toast.innerHTML,/otro cambio manual/);
   assert.match(elements.app.innerHTML,/Lugares ajustados · hasta las 08:00/);
   context.expired=JSON.parse(JSON.stringify(input.fixture));
   context.expired.now=1794664800;
   const ec=context.expired.hosts[0].snapshot.control;
   ec.revision=3;ec.mode='dia';ec.cap=14;ec.expires_at=null;ec.slots={hasito:1,jale:5,'jale-verify':1,otros:2,pesadas:2,rapidas:3};ec.desired=ec.slots;
   run('render(expired);globalThis.h=S.hosts[0]');
   assert.doesNotMatch(elements.app.innerHTML,/Lugares ajustados/);
   assert.equal(run('h.control.cap'),14);assert.equal(run('h.groups.jale.slots'),5);
   run("acceptSlots(slotKey(h),{request:{op:'slots',host:'desk',action_id:'confirm-000001'},group:'jale'},answer)");
   assert.equal(run('h.control.cap'),14);assert.equal(run('h.groups.jale.slots'),5);
   assert.doesNotMatch(elements.toast.innerHTML,/Deshacer/);assert.match(elements.toast.innerHTML,/venció/);
   // A lost POST persists across reload; a late applied GET must obey the same fence.
   context.fetch=async()=>({ok:true,status:200,json:async()=>context.answer});
   run("PENDING.set('@host/desk',{request:{op:'slots',host:'desk',action_id:'confirm-000001'},group:'jale',status:'unknown'});persistActions()");
   const lateReload={...context};vm.createContext(lateReload);vm.runInContext(input.js,lateReload);
   vm.runInContext("render(expired);globalThis.h=S.hosts[0];globalThis.key=slotKey(h);globalThis.a=PENDING.get(key)",lateReload);
   await vm.runInContext('pollAction(key,a)',lateReload);
   assert.equal(vm.runInContext('h.control.revision',lateReload),3);
   assert.equal(vm.runInContext('h.groups.jale.slots',lateReload),5);
   assert.equal(vm.runInContext('PENDING.size',lateReload),0);
   assert.doesNotMatch(elements.toast.innerHTML,/Deshacer/);
   run("SLOT_MESSAGES.clear();acceptSlots(slotKey(h),{request:{op:'slots',host:'desk',action_id:'confirm-000001'},group:'jale'},{status:'aborted',error:{message:'El ajuste venció en el cambio de horario.'}})");
   assert.equal(run('PENDING.size'),0);assert.match(elements.app.innerHTML,/venció/);
   assert.equal(requests.length,1,'unknown must query, never replay POST');
   console.log('UI desk OK: día/noche, mínimo/máximo/tope, pendiente/conflicto/unknown, vencimiento, undo y host antiguo');
   return;
 }
 if(input.scenario==='disabled'){
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('POST answer lost')};
   run("globalThis.sent=null;const originalSendSlots=sendSlots;sendSlots=(...args)=>(globalThis.sent=originalSendSlots(...args));startSlots(h,{group:'suites',delta:-1})");
   await run('sent');
   run('globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)');
   const ident=run('a.request.action_id');
   // A whois/Host/Origin denial from serve.py has no receipt or action_id.
   const disabled={...input.disabled_response,action_id:ident};
   for(const denial of [{schema:'rack-action@1',error:{code:'forbidden'}},
                         {...disabled,action_id:'different-00001'},
                         {...disabled,effects:true},
                         {...disabled,error:{code:'forbidden'}}]){
     context.fetch=async()=>({ok:false,status:403,json:async()=>denial});
     await run('pollAction(key,a)');
     assert.equal(run('PENDING.size'),1);assert.equal(run('a.status'),'unknown');
   }
   const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);vm.runInContext('render(fixture)',reload);
   assert.equal(vm.runInContext('[...PENDING.values()][0].request.action_id',reload),ident);
   context.fetch=async()=>({ok:false,status:403,json:async()=>disabled});
   await run('pollAction(key,a)');
   assert.equal(run('PENDING.size'),0,'confirmed disabled GET must release host lock');
   assert.match(elements.app.innerHTML,/Los ajustes de lugares están apagados/);
   assert.doesNotMatch(elements.app.innerHTML,/Resultado desconocido/);
   assert.deepEqual(JSON.parse(storage['rack-actions'] || '[]'),[]);
   // Fresh capabilities permit another click, always with a new action_id.
   run('render(fixture);globalThis.h=S.hosts[0]');
   assert(!/data-step="-1"[^>]*disabled/.test(run("slotControls(h,'suites',h.groups.suites,0)")));
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('next POST answer lost')};
   run("startSlots(h,{group:'suites',delta:-1})");await run('sent');
   assert.notEqual(run('PENDING.get(slotKey(h)).request.action_id'),ident);
   assert.equal(requests.filter(r=>r[0]==='/api/accion' && JSON.parse(r[1].body).action_id===ident).length,1);
   console.log('UI slots OK: disabled 403 confirmado libera bloqueo; autorización falla cerrada; ID nuevo sin reenviar');
   return;
 }
 assert.match(elements.app.innerHTML,/default/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/<b>3<\/b>/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/0\/3 en uso/);
 assert.match(elements.app.innerHTML,/· tope 7/);
 const disabled={disabled:true,dataset:{step:'1',host:'mini',group:'suites'}};
 for(const [type,fn] of listeners) if(type==='click') fn({target:{closest:selector=>selector==='button[data-step]'?disabled:null}});
 assert.equal(run('PENDING.size'),0);

 assert.match(run("slotControls(h,'default',h.groups.default,0)"),/Mínimo de 1 lugar/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/Tope de 7 lugares. Quita un lugar a otro grupo para añadirlo aquí./);
 assert.match(run("slotControls({...h,control:{...h.control,slots:{suites:8,default:1,rapidas:1}}},'suites',{slots:8},0)"),/Máximo de 8 lugares/);
 assert.match(run("slotControls({...h,control:null},'suites',{slots:3},0)"),/Falta actualizar/);
 assert.match(run("hostControlState({...h,control:{status:'busy',error:{message:'El control está ocupado. Vuelve a intentarlo.'}}})"),/El control está ocupado/);
 assert.doesNotMatch(run("slotControls({...h,control:{status:'busy',error:{message:'El control está ocupado. Vuelve a intentarlo.'}}},'suites',{slots:3},0)"),/pausa/);
 assert.match(run("slotControls({...h,id:'desk'},'suites',{slots:3},0)"),/disabled/);
 assert.match(run("slotControls(h,'suites',{slots:2},4)"),/4\/2 en uso · no entran nuevas hasta bajar de 2/);
 context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('network')};
 run("globalThis.sent=null;const originalSendSlots=sendSlots;sendSlots=(...args)=>(globalThis.sent=originalSendSlots(...args));");
 run("startSlots(h,{group:'suites',delta:-1})"); await run('sent');
 assert.equal(run('PENDING.size'),1);assert.equal(run("PENDING.get(slotKey(h)).status"),'unknown');
 assert.equal(run('h.groups.suites.slots'),3);assert.match(elements.app.innerHTML,/Resultado desconocido/);
 run("globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)");
 const ident=run('a.request.action_id');
 assert.equal(requests.filter(r=>r[0]==='/api/accion').length,1);
 context.fetch=async(url)=>{requests.push([url]);return {ok:false,status:404,json:async()=>({schema:'rack-action@1',action_id:ident,status:'unknown'})}};
 await run('pollAction(key,a)');
 assert.equal(requests.filter(r=>r[0]==='/api/accion').length,1);
 const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);vm.runInContext('render(fixture)',reload);
 assert.equal(vm.runInContext('[...PENDING.values()][0].request.action_id',reload),ident);
 run("acceptAction(key,a,{status:'partial',result:{observed:{suites:3,default:1,rapidas:3},desired:{suites:2,default:1,rapidas:3}}})");
 assert.match(elements.app.innerHTML,/Aplicado parcialmente/);assert.match(elements.app.innerHTML,/3 observado \/ 2 deseado/);
 run("acceptSlots(key,a,{status:'applied',revision:2,result:{observed:{suites:2,default:1,rapidas:3},desired:{suites:2,default:1,rapidas:3}}})");
 assert.equal(run('PENDING.size'),0);assert.equal(run('h.groups.suites.slots'),2);assert.match(elements.toast.innerHTML,/Deshacer/);
 assert(timers.some(t=>t.ms===8000));run('render(fixture)');assert.equal(run('S.hosts[0].groups.suites.slots'),2);
 run("globalThis.h=S.hosts[0];startSlots(h,null,a.request.action_id)");await run('sent');
 assert.equal(run("PENDING.get(slotKey(h)).request.op"),'undo');
 run("globalThis.undo=PENDING.get(slotKey(h));acceptSlots(slotKey(h),undo,{status:'conflict',error:{message:'Los lugares cambiaron. No se puede deshacer.'}})");
 assert.match(elements.app.innerHTML,/Los lugares cambiaron/);assert.equal(run('PENDING.size'),0);
 run("startSlots(h,{group:'suites',delta:-1})"); await run('sent');
 run("globalThis.terminal=PENDING.get(slotKey(h))");
 const terminalId=run('terminal.request.action_id');
 context.fetch=async()=>({ok:false,status:409,json:async()=>({schema:'rack-action@1',action_id:terminalId,status:'rejected',error:{message:'Tope confirmado'}})});
 await run('pollAction(slotKey(h),terminal)');
 assert.equal(run('PENDING.size'),0);
 assert.match(elements.app.innerHTML,/Tope confirmado/);

 console.log('UI slots OK: mínimo/máximo/tope, host antiguo, exceso, pendiente/conflicto/unknown, sin optimismo, parcial visible, reload, GET sin reenvío, undo y toast 8 s');
})().catch(e=>{console.error(e);process.exitCode=1});
